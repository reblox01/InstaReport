"""The browser report channel, as a state machine plus a thin Playwright adapter.

    ┌─────────────────────────── ONE REPORT ───────────────────────────┐
    │                                                                   │
    │   profile ──▶ menu ──▶ reason dialog ──▶ sub-dialog ──▶ submit    │
    │      │           │           │                │          │       │
    │      │           │           │                │      ══╪═══     │
    │      │           │           │                │      dispatch   │
    │      │           │           │                │      boundary   │
    │      │           │           │                │          │       │
    │      ▼           ▼           ▼                ▼          ▼       │
    │   not found / challenge / login wall / rate limit               │
    └───────────────────────────────────────────────────────────────────┘

Three rules govern the module, and each exists because the previous version of
this tool broke it.

1. The network is primary, the DOM corroborates.
   Instagram renders success optimistically, to any reporter, including
   unauthenticated and flagged identities, so a confirmation string is not
   evidence that a report was filed. It is evidence that the UI transitioned.
   The verdict comes from the submit request's status and body
   (:func:`insta_report.outcomes.classify_network`); the DOM only decides
   whether to upgrade UNCONFIRMED to ACKED. This module never decides a terminal
   state -- it produces :class:`~insta_report.outcomes.BrowserEvidence` and
   hands it to :func:`~insta_report.outcomes.classify_browser`, which is a pure
   function with a golden corpus behind it.

2. The dispatch boundary is declared *before* the click, not after.
   ``page.click`` resolves after the request is already in flight, so anything
   observed afterwards is racing a fact we need to have already recorded. The
   channel calls ``on_dispatch()`` immediately before issuing the submit click.
   If the process dies in that window we hold an intent with no outcome, which
   reconciles to UNKNOWN -- never to a retry. The alternative is a report that
   went out with no ledger entry, and a resume that files it a second time.

3. A state transition is asserted, never inferred from one string.
   Every reading records which anchors appeared and which disappeared relative
   to the previous one. "The confirmation text is on the page" is not enough,
   because it can also be there *before* submit: as a dialog heading, as a
   button label, in a mid-wizard explanation. So the terminal check is a set
   difference between what was seen before the boundary and after, which is why
   :class:`Observation` carries anchor *names* for the wizard and marker
   *texts* for the classifier, and why both sets are tracked separately.

Layering
--------
Only :class:`BrowserDriver` imports Playwright. Everything above it -- the
wizard, the classifier, the submit capture, the blocked-state check -- is a pure
function over observations and needs no browser. That split is not tidiness: it
is what lets the state machine, where every trap named in the design lives, be
exercised against a golden fixture in CI, and it keeps exactly one file
responsible for Playwright's own failure modes.

``channel="chromium"``
----------------------
The driver launches the full chromium build rather than the headless shell. The
headless-shell download does not land reliably in every environment, and a
channel that cannot launch cannot report, so depending on an optional download
is the wrong dependency. Pinned in code with a comment rather than in config
because it is a property of the installation, not a preference.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, ClassVar, Protocol, runtime_checkable

from .anchors import AnchorSet
from .artifacts import ArtifactStore, FailureContext
from .errors import (
    AccountChallenged,
    ChannelFailError,
    ErrorScope,
    InstaReportError,
    NavigationTimeout,
    SelectorDrift,
    SessionExpired,
)
from .outcomes import BrowserEvidence, Outcome, TerminalState, classify_browser, utc_now
from .proxies import ProxyLease
from .targets import Target

log = logging.getLogger(__name__)

CHANNEL = "browser"

__all__ = [
    "CHANNEL",
    "Action",
    "BrowserChannel",
    "BrowserDriver",
    "NetworkProbe",
    "Observation",
    "PageDriver",
    "ReportWizard",
    "SubmitCapture",
    "SubmitPolicy",
    "SubmitRequest",
    "WizardState",
    "classify_blocked",
    "classify_observation",
    "observe",
    "submit_url_hints",
]


# ===========================================================================
# 1. What a single reading of the page can tell us
# ===========================================================================


@dataclass(frozen=True)
class Observation:
    """One reading of the page, as a value.

    Frozen, and free of Playwright types, so a test can build one in a line and
    drive the state machine through every sequence without a browser.

    Two parallel sets, on purpose:

    ``anchors_hit``
        Anchor **names**, for the wizard's state classification. Names are
        stable identifiers that a test can write down.

    ``anchor_texts``
        The **marker string** each hit resolved to. This is what
        :func:`~insta_report.outcomes.classify_browser` compares against, and
        it is deliberately not derived from the names: the pre-submit trap is
        about whether a particular piece of *text* was already on the page, and
        answering that question with anchor names would answer a different one
        -- one where a renamed anchor silently looks like a clean page.
    """

    anchors_hit: frozenset[str] = frozenset()
    anchor_texts: frozenset[str] = frozenset()
    #: The report menu entry is present and clickable.
    menu_open: bool = False
    #: The reason list is on the page.
    reason_list_present: bool = False
    #: A category is selected, named. ``None`` when nothing is chosen.
    category_selected: str | None = None
    #: The submit affordance is present.
    submit_present: bool = False
    #: The post-submit request, if one has been captured by now. Later readings
    #: repeat it; the wizard keeps the first.
    submit: SubmitRequest | None = None
    #: Monotonic timestamp, so the wizard can prove a confirmation *persisted*
    #: rather than having flashed past.
    at: float = 0.0
    url: str = ""
    title: str = ""

    def with_anchors(
        self, names: Iterable[str], texts: Iterable[str] = ()
    ) -> "Observation":
        return replace(
            self,
            anchors_hit=self.anchors_hit | frozenset(names),
            anchor_texts=self.anchor_texts | frozenset(texts),
        )

    def with_submit(self, request: "SubmitRequest | None") -> "Observation":
        return replace(self, submit=request)


#: The anchor names this module reasons about. These are the *names* from the
#: anchor file, not attribute names on :class:`~insta_report.anchors.AnchorSet`,
#: and they are the strings that appear in an observation, a transition record,
#: and a failure bundle -- so they are written down once here and referenced
#: everywhere else rather than retyped.
MENU_ITEM = "report_dialog.menu_item"
REASON_LIST = "report_dialog.reason_list"
REASON_ITEM = "report_dialog.reason_item"
REQUIRED_TEXT = "report_dialog.required_text"
SUBHEADING = "report_dialog.subdialog.heading"
SUBITEM = "report_dialog.subdialog.item"
TRIGGER = "report_dialog.trigger"
IDENTITY = "identity"
SUBMIT = "submit"
CONFIRMATION = "confirmation"
NOT_FOUND = "not_found"
CHALLENGE = "challenge"
LOGIN_WALL = "login_wall"
RATE_LIMITED = "rate_limited"

#: Anchors that mean "this page is not a report dialog". Their presence is a
#: *blocker*, never a confirmation.
BLOCKERS: tuple[str, ...] = (CHALLENGE, LOGIN_WALL, RATE_LIMITED, NOT_FOUND)

#: Anchors that mean "a report dialog is on the page but not yet submittable".
PENDING: tuple[str, ...] = (
    TRIGGER,
    MENU_ITEM,
    REASON_LIST,
    REASON_ITEM,
    REQUIRED_TEXT,
    SUBHEADING,
    SUBITEM,
    IDENTITY,
)

#: Checked in this order, and all of them, every reading. A reading that
#: stopped at the first hit could not say "a challenge appeared while the
#: confirmation was still on screen", which is where stopping early costs most.
CHECK_ORDER: tuple[str, ...] = (*BLOCKERS, *PENDING, SUBMIT, CONFIRMATION)

#: Blockers that are a property of the *account or the exit*, not of the target.
#: A challenge means the exit is flagged; a login wall means the session died.
#: Either way the lease is unusable and no further report may leave on it.
ACCOUNT_BLOCKERS: frozenset[str] = frozenset({CHALLENGE, LOGIN_WALL, RATE_LIMITED})
#: A 404 is about the target, not about us.
TARGET_BLOCKERS: frozenset[str] = frozenset({NOT_FOUND})

#: Post-submit requests whose URL contains one of these are treated as *the*
#: report submit. Configurable because the endpoint is Instagram's business; a
#: hardcoded guess would be a second place for the anchors to drift.
DEFAULT_SUBMIT_HINTS: tuple[str, ...] = ("report",)


def submit_url_hints(hints: Iterable[str] | None = None) -> tuple[str, ...]:
    """Normalise configured submit-URL hints, with the default folded in.

    The default is kept even when the operator configures extra hints, because a
    run that silently stops recognising the actual submit endpoint is worse than
    one that occasionally matches an unrelated request. A false positive here is
    recorded in the artifact bundle, not acted on -- the classification still
    requires a well-formed response body.
    """
    return tuple(dict.fromkeys((*DEFAULT_SUBMIT_HINTS, *(hints or ()))))


# ===========================================================================
# 2. The submit request capture
# ===========================================================================


@dataclass(frozen=True)
class SubmitRequest:
    """One captured request that might be the submit."""

    url: str
    method: str = ""
    status: int | None = None
    content_type: str | None = None
    body: str = ""
    #: A body was read but the read failed partway. Reported rather than
    #: swallowed, because a truncated body is a reason a verdict gets
    #: downgraded, and the operator needs to know that is the reason.
    body_truncated: bool = False
    error: str = ""
    #: Every other post-submit request, so a human can see what the choice was
    #: made from. A ledger entry reading "200 OK" with no request attached is
    #: the exact shape of the original tool's bug.
    siblings: tuple["SubmitRequest", ...] = ()

    @property
    def is_mutation(self) -> bool:
        """A submit is never a GET, so this is the one rule we can trust blind."""
        return self.method.upper() not in ("GET", "HEAD", "")

    def redacted(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "method": self.method,
            "status": self.status,
            "content_type": self.content_type,
            "body_excerpt": self.body[:2000],
            "body_truncated": self.body_truncated,
            "error": self.error,
            "sibling_count": len(self.siblings),
        }


class SubmitCapture:
    """Accumulates post-click requests and names the one that submitted.

    Selection, in order:

    1. exactly one non-GET request whose URL matches a configured hint;
    2. exactly one non-GET request at all, because a form dispatching a single
       XHR after a click is dispatching that XHR;
    3. nothing, ever. Zero unambiguous candidates is a real answer.

    Rule 3 is the one that matters. A submit we did not capture is not a
    successful submit, and it is not a failed one either -- it is UNKNOWN, and
    the DOM is not allowed to stand in for a response that never arrived.
    """

    def __init__(self, hints: Sequence[str] | None = None) -> None:
        self._hints = tuple(h.lower() for h in submit_url_hints(hints))
        self._records: list[SubmitRequest] = []

    def add(self, request: SubmitRequest) -> None:
        self._records.append(request)

    def __bool__(self) -> bool:
        return bool(self._records)

    def __len__(self) -> int:
        return len(self._records)

    @property
    def all_requests(self) -> tuple[SubmitRequest, ...]:
        return tuple(self._records)

    def select(self) -> SubmitRequest | None:
        """The request that submitted, or ``None`` with the reasons recorded.

        Both rules filter on :attr:`SubmitRequest.is_mutation` first. A GET is
        not a submit however suggestive its URL is, and ``/report/feed/`` is a
        read of *other people's reports* -- a page that is on screen while the
        dialog is open. Accepting it because the path contains "report" would
        put a 200 from an unrelated feed in the ledger as the verdict.
        """
        hinted = [
            r for r in self._records if r.is_mutation and self._matches_hint(r)
        ]
        if len(hinted) == 1:
            return _with_siblings(hinted[0], self._records)
        mutations = [r for r in self._records if r.is_mutation]
        if len(mutations) == 1:
            return _with_siblings(mutations[0], self._records)
        if hinted:
            log.warning(
                "post-submit capture has %d requests matching a submit hint; "
                "refusing to choose between them. The verdict will be graded "
                "network-unusable rather than guessed from one of them.",
                len(hinted),
            )
        return None

    def _matches_hint(self, request: SubmitRequest) -> bool:
        haystack = f"{request.method} {request.url}".lower()
        return any(hint in haystack for hint in self._hints)

    def explain(self) -> str:
        """Why the capture came out as it did, for an artifact bundle."""
        if not self._records:
            return "no post-submit request was captured at all"
        chosen = self.select()
        if chosen is None:
            return (
                f"{len(self._records)} post-submit request(s), none "
                "unambiguously identifiable as the submit"
            )
        return f"chose {chosen.method} {chosen.url} from {len(self._records)} captured"


def _with_siblings(
    chosen: SubmitRequest, records: Sequence[SubmitRequest]
) -> SubmitRequest:
    return replace(chosen, siblings=tuple(r for r in records if r is not chosen))


# ===========================================================================
# 3. Classifying one observation into a state
# ===========================================================================


class WizardState(str, Enum):
    """Where the wizard believes it is.

    ``EMPTY`` is the start: a page with nothing reportable on it. It is a
    distinct state rather than a flavour of "not found" because a profile that
    is still loading looks exactly like an empty page, and the two need
    different responses -- one is a wait, the other is a dead target.
    """

    EMPTY = "empty"
    PROFILE = "profile"
    MENU_OPEN = "menu_open"
    REASON_DIALOG = "reason_dialog"
    SUBDIALOG = "subdialog"
    READY = "ready"
    DISPATCHED = "dispatched"
    CONFIRMED = "confirmed"
    BLOCKED = "blocked"
    GONE = "gone"


def classify_blocked(hit: Iterable[str]) -> frozenset[str]:
    """Which blockers are present, restricted to the four we understand."""
    return frozenset(hit) & frozenset(BLOCKERS)


def classify_observation(observation: Observation) -> WizardState:
    """One observation in, one state out.

    Ordered by severity rather than by how common the state is, because the
    expensive mistakes are the ones where a page that is *not* a report dialog
    gets read as one. A blocker beats a confirmation beats a pending anchor,
    every time.

    ``GONE`` and ``BLOCKED`` are separate states even though both are terminal
    and neither is submittable, because they call for opposite handling: a
    missing target is a fact about the target and every channel will see it
    identically, while a challenge is a fact about the account and the exit and
    must cost the lease. Collapsing them would mean either quarantining every
    deleted account or failing to quarantine every CAPTCHA.
    """
    if NOT_FOUND in observation.anchors_hit:
        return WizardState.GONE
    if classify_blocked(observation.anchors_hit) & ACCOUNT_BLOCKERS:
        return WizardState.BLOCKED
    if CONFIRMATION in observation.anchors_hit:
        return WizardState.CONFIRMED
    dialogish = (
        observation.menu_open
        or observation.reason_list_present
        or observation.category_selected
        or observation.submit_present
        or bool(observation.anchors_hit & frozenset(PENDING))
    )
    if not dialogish:
        return WizardState.EMPTY
    if observation.category_selected and observation.submit_present:
        return WizardState.READY
    if observation.reason_list_present:
        return WizardState.REASON_DIALOG
    if observation.category_selected:
        return WizardState.SUBDIALOG
    if observation.menu_open:
        return WizardState.MENU_OPEN
    return WizardState.PROFILE


# ===========================================================================
# 4. The state machine
# ===========================================================================


class Action(str, Enum):
    """What the wizard wants done next. Pure data; the driver executes it."""

    NONE = "none"
    OPEN_MENU = "open_menu"
    PICK_MENU_ITEM = "pick_menu_item"
    CHOOSE_CATEGORY = "choose_category"
    SUBMIT = "submit"
    WAIT = "wait"


#: States from which no click is possible. Reaching one of these having
#: clicked nothing is a channel failure, not a report outcome.
TERMINAL_UNCLICKED: frozenset[WizardState] = frozenset(
    {WizardState.BLOCKED, WizardState.GONE, WizardState.CONFIRMED}
)


@dataclass
class ReportWizard:
    """The wizard, as a value.

    One instance per report attempt. Holds the anchor snapshot, the transition
    log, and the dispatch flag -- no Playwright, no I/O, and no clock of its own
    (the clock arrives in each observation). That is what makes every trap in
    the design testable by constructing an observation.

    Invariants enforced here
    ------------------------
    * A click is only ever issued from a state with a specific affordance, so a
      click can never be aimed at nothing.
    * The dispatch boundary is set once, on the first click of a submit
      affordance, and is never cleared.
    * A submit affordance reappearing after the boundary is recorded, and **no
      second click is issued** -- see :meth:`note_post_boundary_ready`.
    * Every transition records what appeared and what disappeared relative to
      the previous reading, so "it advanced" is always answerable with evidence.
    """

    #: Every anchor the wizard has ever seen, by anchor **name**. The terminal
    #: DOM check is a set difference over marker *texts* (:attr:`pre_submit` and
    #: :attr:`post_submit`); this set is what the failure bundle and the log talk
    #: about, because a name is something a human can act on.
    seen: set[str] = field(default_factory=set)
    transitions: list[dict[str, Any]] = field(default_factory=list)
    clicks: int = 0
    dispatched: bool = False
    #: Marker **texts** seen before the boundary, and after it. The pair is what
    #: the classifier compares, and keeping texts rather than names is the point:
    #: the pre-submit trap is about whether a particular piece of text was
    #: already on the page, and substituting names for texts would answer a
    #: different question -- one where renaming an anchor silently looks like a
    #: clean page.
    pre_submit: frozenset[str] = frozenset()
    post_submit: frozenset[str] = frozenset()
    #: Anchor **names** seen after the boundary, for the failure bundle.
    post_seen: set[str] = field(default_factory=set)
    #: A submit affordance appeared again after the boundary.
    post_boundary_ready: bool = False
    #: A confirmation seen in two consecutive readings, so a toast that flashes
    #: past cannot be mistaken for the wizard having finished.
    confirmation_stable: bool = False
    #: The first post-submit request seen, kept so the network verdict does not
    #: change as more responses arrive.
    submit: SubmitRequest | None = None
    #: Why category selection refused, when it did. A field rather than an
    #: attribute bolted on afterwards, so the reason is part of the attempt's
    #: record instead of a side channel.
    category_failure: str | None = None
    #: Reading budget, so a page that never converges raises rather than
    #: spinning until the navigation timeout and being reported as a timeout.
    max_observations: int = 40

    def __post_init__(self) -> None:
        if self.max_observations < 4:
            raise ValueError(
                "max_observations must leave room for a profile, a menu, a "
                "dialog, and one post-submit reading; below 4 the wizard would "
                "give up on a flow that works"
            )
        self.state = WizardState.EMPTY
        self._last_hit: frozenset[str] = frozenset()
        self._last_state: WizardState = WizardState.EMPTY
        self._confirm_streak = 0

    # -- reading the page -------------------------------------------------

    def observe(self, observation: Observation) -> WizardState:
        """Fold one reading in and return the state it implies.

        The order here is load-bearing. ``appeared`` is computed against
        ``self.seen`` *before* this reading is merged, so "this anchor is new"
        means new to the wizard rather than new to this instant. Getting that
        backwards would make the first reading of every page look like a
        transition.
        """
        before = frozenset(self.seen)
        state = classify_observation(observation)
        if state is WizardState.CONFIRMED:
            self._confirm_streak = (
                self._confirm_streak + 1
                if self._last_state is WizardState.CONFIRMED
                else 1
            )
        else:
            self._confirm_streak = 0
        self.confirmation_stable = self._confirm_streak >= 2

        if self.dispatched:
            self.post_submit |= observation.anchor_texts
            self.post_seen |= observation.anchors_hit
            if state is WizardState.READY:
                self.note_post_boundary_ready()
        else:
            self.pre_submit |= observation.anchor_texts

        if self.submit is None and observation.submit is not None:
            self.submit = observation.submit

        self.transitions.append(
            {
                "from": self._last_state.value,
                "to": state.value,
                "appeared": sorted(observation.anchors_hit - before),
                "disappeared": sorted(self._last_hit - observation.anchors_hit),
                "submit_present": observation.submit_present,
                "category": observation.category_selected,
                "submit": observation.submit.redacted() if observation.submit else None,
                "post": self.dispatched,
                "at": round(observation.at, 3),
            }
        )
        self.seen |= observation.anchors_hit
        self._last_hit = observation.anchors_hit
        self._last_state = state
        self.state = state
        return state

    # -- deciding ---------------------------------------------------------

    def action(self) -> Action:
        """What to do next, or ``NONE`` if nothing is possible."""
        if self.dispatched:
            # Past the boundary, so the only remaining action is to wait. The one
            # click that is left is the one that would risk a second dispatch.
            return Action.WAIT
        if self.state in TERMINAL_UNCLICKED:
            return Action.NONE
        if self.state is WizardState.READY:
            return Action.SUBMIT
        if self.state is WizardState.MENU_OPEN:
            return Action.PICK_MENU_ITEM
        if self.state in (WizardState.REASON_DIALOG, WizardState.SUBDIALOG):
            return Action.CHOOSE_CATEGORY
        if self.state is WizardState.PROFILE:
            return Action.OPEN_MENU
        return Action.WAIT

    def budget_exhausted(self) -> bool:
        return len(self.transitions) >= self.max_observations

    def note_category_failure(self, reason: str) -> None:
        self.category_failure = reason

    # -- the boundary -----------------------------------------------------

    def declare_dispatch(self) -> None:
        """Cross the dispatch boundary. Idempotent; never reversed.

        Called immediately *before* the click that could submit. The pre-submit
        snapshot is frozen here rather than read at the end, because after the
        click the DOM may already have advanced -- and a snapshot taken then
        would contain the very answer it exists to be tested against. The
        contents are already correct at this point: every reading before the
        boundary has been accumulating into :attr:`pre_submit`, and the click
        has not happened, so nothing after it can have contributed.
        """
        if self.dispatched:
            return
        self.dispatched = True
        self.pre_submit = frozenset(self.pre_submit)
        self.transitions.append(
            {
                "from": self.state.value,
                "to": self.state.value,
                "dispatched": True,
                "post": True,
                "at": None,
            }
        )

    def note_post_boundary_ready(self) -> None:
        """A submit affordance reappeared after the boundary.

        This is the multi-step transition trap. A two-stage confirm -- pick a
        reason, then confirm the pick -- presents a second submit button after
        the first click. Nothing in the DOM distinguishes "the first click
        submitted" from "the first click advanced the wizard", so no second click
        is issued. The result is UNKNOWN by construction, which is the honest
        answer: two plausible worlds, no way to tell them apart, therefore no
        claim. Recorded rather than raised so the artifact bundle can show the
        page that caused it.
        """
        self.post_boundary_ready = True

    def note_click(self) -> None:
        self.clicks += 1

    @property
    def observations(self) -> int:
        return len(self.transitions)

    # -- the finished attempt ---------------------------------------------

    def evidence(self) -> BrowserEvidence:
        """Assemble what the classifier needs. No verdict is formed here.

        ``submit_affordance_gone`` is ``True`` only when a reading taken *after*
        the boundary actually found the affordance absent. Before the boundary
        it is ``None``, not ``False``: "the button is not there" is not a claim
        about a post-submit DOM, and answering it anyway would let a channel
        that never submitted look like one whose UI advanced.

        ``interstitial`` is set from what was seen *after* the boundary only.
        A challenge on the profile page is a pre-dispatch failure the channel
        has already turned into a quarantine by the time evidence is assembled;
        a challenge that appears *after* the click is a different thing, and it
        is the one the classifier is built to raise rather than return, so
        flagging it here is what gets the account pulled instead of the target
        being re-reported on another channel.

        ``confirmation_stable`` is the wizard's own answer to "did the
        confirmation persist", and it is passed explicitly because the anchor
        sets here are cumulative: without it a single-reading toast would be
        indistinguishable from a finished wizard.
        """
        post = [t for t in self.transitions if t.get("post") and "to" in t]
        gone: bool | None = None
        interstitial = False
        if self.dispatched and post:
            gone = post[-1].get("submit_present") is False
            interstitial = bool(classify_blocked(self.post_seen) & ACCOUNT_BLOCKERS)
        return BrowserEvidence(
            dispatched=self.dispatched,
            pre_submit_anchors=self.pre_submit,
            post_submit_anchors=self.post_submit,
            submit_affordance_gone=gone,
            network_status=self.submit.status if self.submit else None,
            network_body=self.submit.body if self.submit else None,
            network_content_type=self.submit.content_type if self.submit else None,
            interstitial=interstitial,
            confirmation_stable=self.confirmation_stable,
        )

    def summary(self) -> str:
        """One line for a log, naming the path rather than only the end state."""
        path = " -> ".join(self.path())
        return (
            f"clicks={self.clicks} dispatched={self.dispatched} "
            f"post_boundary_ready={self.post_boundary_ready} "
            f"confirmation_stable={self.confirmation_stable} path=[{path}]"
        )

    def path(self) -> list[str]:
        """The states the wizard was actually *observed* in, in order.

        The dispatch boundary is excluded: it is a record of a decision, not a
        reading, and leaving it in makes a single READY look like two, which
        reads as the multi-step trap even on a clean single-submit flow.
        """
        return [
            t["to"]
            for t in self.transitions
            if "to" in t and not t.get("dispatched")
        ]


# ===========================================================================
# 5. The default way to read a page: through the anchors
# ===========================================================================


@runtime_checkable
class NetworkProbe(Protocol):
    """How to satisfy one anchor against a live page.

    Async, and injected, so :func:`observe` has no Playwright dependency and a
    test can substitute a probe that asserts on the anchor name and returns
    marker text. ``require_enabled`` matters because Instagram keeps a disabled
    submit button on the page while the form is incomplete: matching it would
    make a page that cannot be submitted look ready.
    """

    async def __call__(
        self, name: str, *, require_enabled: bool, timeout_ms: int | None
    ) -> str | None:
        """Return the marker if ``name`` is satisfied, else ``None``."""


async def observe(
    anchors: AnchorSet,
    probe: NetworkProbe,
    *,
    category_probe: Any | None = None,
    submit: SubmitRequest | None = None,
    at: float = 0.0,
) -> Observation:
    """One reading of the page, built by checking every anchor.

    Every anchor is asked, in a fixed order, so a reading reports the complete
    set rather than stopping at the first hit.
    """
    names: set[str] = set()
    texts: set[str] = set()
    for name in CHECK_ORDER:
        anchor = anchor_by_name(anchors, name)
        try:
            marker = await probe(
                name,
                require_enabled=anchor.require_enabled,
                timeout_ms=anchor.timeout_ms,
            )
        except Exception:  # noqa: BLE001 - a probe failure is a missing anchor
            log.debug("anchor %s could not be checked", name, exc_info=True)
            continue
        if marker is not None:
            names.add(name)
            texts.add(marker)

    category = None
    if category_probe is not None:
        try:
            category = await category_probe()
        except Exception:  # noqa: BLE001
            log.debug("category probe failed", exc_info=True)

    return Observation(
        anchors_hit=frozenset(names),
        anchor_texts=frozenset(texts),
        menu_open=MENU_ITEM in names,
        reason_list_present=REASON_LIST in names,
        category_selected=category,
        submit_present=SUBMIT in names,
        submit=submit,
        at=at,
    )


def anchor_by_name(anchors: AnchorSet, name: str) -> Any:
    """The one place an anchor name becomes an anchor."""
    for anchor in anchors.all_anchors():
        if anchor.name == name:
            return anchor
    raise SelectorDrift(f"the anchor file has no anchor named {name!r}")


# ===========================================================================
# 6. Which category to pick, and when to refuse
# ===========================================================================


#: The default maximum narrative detail pasted into the dialog, and a floor for
#: category matching. See :meth:`SubmitPolicy._matches` for why the latter is a
#: prefix length rather than a stem count.
MIN_CATEGORY_PREFIX = 6


class SubmitPolicy:
    """Chooses a category from what the *live dialog* offers.

    Configured categories are a hint for matching, never the filed value. The
    report is always filed under the dialog's own spelling, because a
    configured list means every report is filed under whatever the operator
    typed months ago, and a renamed row becomes a silent misclassification.

    Refusing matters as much as choosing. When nothing matches, the policy
    returns ``None`` and the channel records a category failure rather than
    clicking the first row: Instagram's first row is "It's spam" far more often
    than it is what the operator meant.
    """

    def __init__(
        self,
        preferred: Sequence[str] = (),
        *,
        avoid: Sequence[str] = (),
        max_detail: int = 400,
    ) -> None:
        self._preferred = tuple(p.strip().casefold() for p in preferred if p.strip())
        self._avoid = tuple(a.strip().casefold() for a in avoid if a.strip())
        self.max_detail = max_detail

    def choose(
        self, offered: Sequence[str], *, detail: str = ""
    ) -> tuple[str | None, str]:
        """Pick a category and say why. ``None`` means *do not click anything*."""
        options = [o.strip() for o in offered if o and o.strip()]
        if not options:
            return None, "the dialog offered no categories"
        excluded = [o for o in options if self._is_avoided(o)]
        candidates = [o for o in options if o not in excluded]
        if not candidates:
            return None, (
                "every offered category was excluded as self-reporting: "
                + ", ".join(excluded)
            )
        if detail and len(detail) > self.max_detail:
            log.debug(
                "detail longer than %d characters for the report dialog (%d)",
                self.max_detail,
                len(detail),
            )
        if not self._preferred:
            return candidates[0], "no preference configured; took the first offered"
        for wanted in self._preferred:
            for option in candidates:
                if self._matches(wanted, option):
                    return option, f"matched preferred {wanted!r}"
        return None, (
            "no preferred category is offered by this dialog (offered: "
            + ", ".join(candidates)
            + "); refusing to guess"
        )

    def _is_avoided(self, option: str) -> bool:
        folded = option.casefold()
        return any(bad in folded for bad in self._avoid)

    @staticmethod
    def _matches(wanted: str, option: str) -> bool:
        """Does the operator's preference refer to this row?

        Substring either way, plus a shared-prefix rule. The prefix rule exists
        for one reason: Instagram's rows are sentences ("Impersonating an
        account or business") while operators configure slugs ("impersonation"),
        and no substring test relates those two -- "impersonation" is not inside
        "impersonating", nor the reverse. Without it the policy would refuse on
        genuine matches, and a refusal that fires on every real case stops being
        a safety property and becomes a nuisance the operator routes around.

        The prefix has a minimum length so a short configured word cannot sweep up
        half the dialog. Matching is a guess about which row the operator meant,
        and a guess is only acceptable when it is a *specific* one.
        """
        short, long_ = wanted.casefold(), option.casefold()
        if short in long_ or long_ in short:
            return True
        shared = 0
        for left, right in zip(short, long_):
            if left != right:
                break
            shared += 1
        return shared >= MIN_CATEGORY_PREFIX


# ===========================================================================
# 7. What the channel needs from a page
# ===========================================================================


@runtime_checkable
class PageDriver(Protocol):
    """Everything the channel needs from a page.

    Nine methods and no Playwright types. The interface is this small on
    purpose: it is the whole seam that lets the wizard, the policy, and the
    classifier be tested against a golden fixture with no browser, and it means
    a Playwright upgrade cannot reach past this point without someone noticing
    that a method changed shape.
    """

    async def goto_profile(self, handle: str) -> None: ...
    async def read(self, name: str, *, require_enabled: bool) -> str | None: ...
    async def selected_category(self) -> str | None: ...
    async def offered_categories(self) -> tuple[str, ...]: ...
    async def open_menu(self) -> None: ...
    async def pick_menu_item(self) -> None: ...
    async def choose_category(self, category: str) -> None: ...
    async def click_submit(self) -> None: ...
    async def html(self) -> str: ...
    async def close(self) -> None: ...


# ===========================================================================
# 8. Playwright, and only Playwright
# ===========================================================================


@dataclass
class BrowserDriver:
    """Playwright behind the :class:`PageDriver` seam.

    The only class in the project that imports Playwright, and the only place
    that knows about pages, contexts, user-data directories, response events,
    and the launch flag. Everything it does that is not literally Playwright is
    a translation: DOM into marker text, responses into :class:`SubmitRequest`.
    """

    user_data_dir: Any
    headless: bool = True
    locale: str = "en-US"
    timezone_id: str | None = None
    navigation_timeout_ms: int = 30_000
    confirmation_timeout_ms: int = 15_000
    #: A local fixture URL instead of a live profile. Set only by the fixture
    #: tests, and it is the reason the wizard's traps can be tested at all.
    profile_url: str | None = None
    #: Max body bytes read from a response before the rest is abandoned.
    max_body_bytes: int = 64 * 1024
    #: Set when a persistent context could not be opened, so the next call
    #: fails immediately with the real reason instead of trying again.
    _session_error: str | None = field(default=None, repr=False)

    _playwright: Any = field(default=None, repr=False)
    _browser: Any = field(default=None, repr=False)
    _context: Any = field(default=None, repr=False)
    _page: Any = field(default=None, repr=False)
    _capture: SubmitCapture = field(default_factory=SubmitCapture, repr=False)
    _anchors: AnchorSet = field(default=None, repr=False)
    _closing: bool = field(default=False, repr=False)

    # -- lifecycle --------------------------------------------------------

    async def __aenter__(self) -> "BrowserDriver":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self, anchors: AnchorSet) -> "BrowserDriver":
        """Launch, open a persistent context, and attach the capture.

        The user data directory is why a persistent context is used at all. A
        ``sessionid`` alone cannot reconstruct a session: Instagram reads the
        cookie alongside a matching device fingerprint, and a
        header-reconstructed session is exactly the shape it flags. A real
        directory holds the whole browser profile, so the session arrives as the
        browser state it actually is.
        """
        if self._browser is not None:
            return self
        self._anchors = anchors
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ChannelFailError(
                "playwright is not installed, so the browser channel cannot "
                "serve. Install it, or run with --no-browser to fall through "
                "to the API channel.",
                scope=ErrorScope.RUN,
            ) from exc

        self._playwright = await async_playwright().start()
        context_options: dict[str, Any] = {
            "locale": self.locale,
            "timezone_id": self.timezone_id,
            "viewport": {"width": 1280, "height": 900},
        }
        # The full chromium build, not the headless shell: the shell is an
        # optional download that does not land reliably everywhere, and a
        # channel that cannot launch cannot report.
        launch_options: dict[str, Any] = {
            "channel": "chromium",
            "headless": self.headless,
        }
        try:
            if self.user_data_dir:
                # A persistent context launches and owns its browser, so there
                # is no separate browser object to close -- and no way to add
                # a user data directory to a context that already exists, which
                # is why this is not just ``new_context(user_data_dir=...)``.
                # Passing that to new_context is a TypeError, and it is the
                # kind that only a real launch finds.
                self._context = (
                    await self._playwright.chromium.launch_persistent_context(
                        str(self.user_data_dir),
                        **launch_options,
                        **context_options,
                    )
                )
            else:
                self._browser = await self._playwright.chromium.launch(**launch_options)
                self._context = await self._browser.new_context(**context_options)
        except Exception as exc:
            # With a user data directory, a failure here means the profile is
            # unusable -- almost always a stale lock from a killed run. Say
            # that, rather than letting a bare PlaywrightError reach the
            # operator, who would otherwise read it as a broken install.
            self._session_error = str(exc)
            await self._shutdown_browser()
            if not self.user_data_dir:
                raise ChannelFailError(
                    f"chromium did not launch: {exc}. If the headless shell is "
                    "missing, run `playwright install chromium` -- the driver "
                    "pins the full build on purpose.",
                    scope=ErrorScope.RUN,
                ) from exc
            raise SessionExpired(
                f"the browser profile at {self.user_data_dir} could not be "
                f"opened: {exc}. A run that was killed leaves a lock file "
                "there; remove the directory if no browser is using it."
            ) from exc

        self._context.set_default_timeout(self.navigation_timeout_ms)
        self._page = await self._context.new_page()
        # Attached once, never removed. A handler detached mid-run would stop
        # recording requests, which is the same class of silent failure as the
        # original tool.
        self._page.on("response", self._on_response)
        return self

    async def close(self) -> None:
        """Shut down, tolerating a half-built stack.

        Every step checks before acting, so cleaning up after a failed start
        cannot raise and mask the original error -- which is how the real reason
        for a failure gets lost.
        """
        if self._closing:
            return
        self._closing = True
        try:
            if self._context is not None:
                await self._context.close()
        except Exception:  # noqa: BLE001
            log.debug("context close failed", exc_info=True)
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:  # noqa: BLE001
            log.debug("browser close failed", exc_info=True)
        await self._shutdown_playwright()
        self._page = self._context = self._browser = self._playwright = None
        self._closing = False

    async def _shutdown_browser(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception:  # noqa: BLE001
            log.debug("browser close during failed start failed", exc_info=True)
        self._browser = None
        await self._shutdown_playwright()

    async def _shutdown_playwright(self) -> None:
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:  # noqa: BLE001
            log.debug("playwright stop failed", exc_info=True)
        self._playwright = None

    # -- response capture -------------------------------------------------

    def _on_response(self, response: Any) -> None:
        """Page event handler. Reads only XHR and fetch.

        A document response to a navigation is not the submit, and letting
        profile HTML into the candidate set would give a form that issues one
        XHR two candidates -- at which point the selection rule declines to
        choose, turning a working report into UNKNOWN.
        """
        if self._page is None:
            return
        try:
            if response.request.resource_type not in ("xhr", "fetch"):
                return
            asyncio.create_task(self._record_response(response))
        except Exception:  # noqa: BLE001
            log.debug("response handler failed", exc_info=True)

    async def _record_response(self, response: Any) -> None:
        """Body reads are async, so this runs as a task on the loop.

        The task is fire-and-forget on purpose: a body read that hangs must not
        hang the wizard. The alternative -- awaiting the capture inline -- would
        put a network read on the critical path of every state transition.
        """
        body = ""
        truncated = False
        error = ""
        try:
            raw = await asyncio.wait_for(
                response.body(), timeout=self.confirmation_timeout_ms / 1000
            )
            if len(raw) > self.max_body_bytes:
                raw = raw[: self.max_body_bytes]
                truncated = True
            body = raw.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            # A body we could not read is why a verdict gets downgraded, and it
            # goes in the record so the operator can see that is the reason,
            # rather than the verdict resting silently on the status code.
            error = f"{type(exc).__name__}: {exc}"
            truncated = True
        headers: dict[str, str] = {}
        try:
            headers = await response.all_headers()
        except Exception:  # noqa: BLE001
            pass
        self._capture.add(
            SubmitRequest(
                url=response.url,
                method=response.request.method,
                status=response.status,
                content_type=headers.get("content-type"),
                body=body,
                body_truncated=truncated,
                error=error,
            )
        )

    @property
    def capture(self) -> SubmitCapture:
        return self._capture

    def drain_capture(self) -> SubmitRequest | None:
        """The submit request seen so far, if unambiguous."""
        return self._capture.select()

    # -- anchor reading ---------------------------------------------------

    async def read(self, name: str, *, require_enabled: bool) -> str | None:
        """Satisfy one anchor against the live page, or return ``None``.

        An anchor is satisfied by *either* half of its definition: a selector
        that matches, or text that is present. A selector hit returns the
        anchor's own configured text as the marker rather than the element's
        text -- the caller only needs to know it matched, and returning page
        text here would let a selector hit satisfy a *text* anchor by accident.
        """
        if self._page is None:
            return None
        anchor = anchor_by_name(self._anchors, name)
        marker = anchor.texts[0] if anchor.texts else name
        for selector in anchor.selectors:
            try:
                locator = self._page.locator(selector).first
                if await locator.count() == 0:
                    continue
                if require_enabled and not await locator.is_enabled():
                    continue
                return marker
            except Exception:  # noqa: BLE001
                continue
        if anchor.texts or anchor.alternate_texts:
            try:
                text = await self._page.inner_text("body")
            except Exception:  # noqa: BLE001
                return None
            if anchor.text_matches(text, self._anchors.normalization) is not None:
                return marker
        return None

    # -- page actions -----------------------------------------------------

    async def goto_profile(self, handle: str) -> None:
        if self._session_error:
            raise SessionExpired(f"browser profile unusable: {self._session_error}")
        url = self.profile_url or f"https://www.instagram.com/{handle}/"
        try:
            await self._page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            if _looks_like_timeout(exc):
                raise NavigationTimeout(
                    f"the profile page for {handle!r} did not settle within "
                    f"{self.navigation_timeout_ms}ms"
                ) from exc
            raise

    async def open_menu(self) -> None:
        await self._click_first(self._anchors.report_dialog_trigger.selectors)

    async def pick_menu_item(self) -> None:
        await self._click_first(self._anchors.report_menu_item.selectors)

    async def choose_category(self, category: str) -> None:
        """Click the row whose visible text matches the chosen category.

        Matching is re-done here rather than trusted from the earlier read,
        because a category chosen from a list read one moment ago has to be
        clicked in the list as it is *now*. A dialog that changed in between is
        a real failure mode, and clicking whatever happens to be at that index
        is how a report gets filed under the wrong category.
        """
        for selector in self._category_selectors():
            locator = self._page.locator(selector)
            try:
                count = await locator.count()
            except Exception:  # noqa: BLE001
                continue
            for index in range(count):
                item = locator.nth(index)
                try:
                    text = (await item.inner_text()).strip()
                except Exception:  # noqa: BLE001
                    continue
                if text.casefold() == category.casefold():
                    await item.click(timeout=self.navigation_timeout_ms)
                    return
        raise SelectorDrift(
            f"category {category!r} was chosen from the dialog's own list but "
            "no row matched it on click; the dialog changed between reading and "
            "clicking",
            missing=(self._anchors.subdialog_item.name,),
        )

    def _category_selectors(self) -> tuple[str, ...]:
        return tuple(self._anchors.subdialog_item.selectors) or tuple(
            self._anchors.reason_item.selectors
        )

    async def click_submit(self) -> None:
        await self._click_first(self._anchors.submit.selectors)

    async def _click_first(self, selectors: Sequence[str]) -> None:
        if self._page is None:
            raise ChannelFailError("no page is open", scope=ErrorScope.RUN)
        last: Exception | None = None
        for selector in selectors:
            try:
                locator = self._page.locator(selector).first
                if await locator.count() == 0:
                    continue
                await locator.click(timeout=self.navigation_timeout_ms)
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                continue
        if last is not None and _looks_like_timeout(last):
            raise NavigationTimeout(f"no clickable match for {list(selectors)}")
        raise SelectorDrift(f"nothing matched any of {list(selectors)}")

    async def offered_categories(self) -> tuple[str, ...]:
        found: list[str] = []
        for selector in self._category_selectors():
            try:
                locator = self._page.locator(selector)
                for index in range(await locator.count()):
                    text = (await locator.nth(index).inner_text()).strip()
                    if text:
                        found.append(text)
            except Exception:  # noqa: BLE001
                continue
        return tuple(dict.fromkeys(found))

    async def selected_category(self) -> str | None:
        """Read back what is selected, rather than assuming the click landed.

        A click that does not select is one of the ways a report is filed under
        no category at all, and Instagram accepts that silently. So the value is
        read from the page, and it is the read-back that the wizard records: a
        click is a request, not an outcome.
        """
        for selector in (
            "[role='radio'][aria-checked='true']",
            "[aria-checked='true']",
            "input[type='radio']:checked ~ *",
        ):
            try:
                locator = self._page.locator(selector).first
                if await locator.count() == 0:
                    continue
                text = (await locator.inner_text()).strip()
                if text:
                    return text
            except Exception:  # noqa: BLE001
                continue
        return None

    async def html(self) -> str:
        if self._page is None:
            return ""
        try:
            return await self._page.content()
        except Exception:  # noqa: BLE001
            return ""


def _looks_like_timeout(exc: Exception) -> bool:
    """Playwright signals a timeout with its own type, and with a message."""
    return "Timeout" in type(exc).__name__ or "timeout" in str(exc).lower()


# ===========================================================================
# 9. The rung the runner stands on
# ===========================================================================


@dataclass
class BrowserChannel:
    """One report attempt through the browser.

    Produces an :class:`~insta_report.outcomes.Outcome` and never invents a
    terminal state: every state it returns is either the output of
    :func:`~insta_report.outcomes.classify_browser` or the documented
    consequence of never dispatching. The ladder lives in the runner; this is
    one rung of it.
    """

    #: The name this channel is filed under, in the ladder, in the checkpoint,
    #: and in the per-channel accounting. Part of the runner's channel
    #: protocol, so it is declared here rather than passed in -- a channel that
    #: had to be told its own name would eventually disagree with itself. A
    #: ClassVar rather than a field, because it is a constant of the channel
    #: type and not a per-run choice.
    name: ClassVar[str] = "browser"

    driver: PageDriver
    anchors: AnchorSet
    policy: SubmitPolicy
    artifacts: ArtifactStore | None = None
    capture: SubmitCapture = field(default_factory=SubmitCapture)
    #: Post-submit readings taken while waiting for the DOM to settle.
    confirmation_polls: int = 3
    #: Delay between readings. Passed in rather than hardcoded so tests can set
    #: it to zero; the real value is a property of page rendering, not of this
    #: module.
    poll_interval: float = 0.5
    #: Set to "url" to make a real report go out over a file:// fixture.
    #: Only the fixture tests set it; a real run leaves it at the live profile.
    confirm_marker: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.confirm_marker = self.anchors.confirmation_text()

    # -- the report -------------------------------------------------------

    async def report(
        self,
        target: Target,
        *,
        on_dispatch: Any,
        account_ref: str | None = None,
        lease: ProxyLease | None = None,
        attempt: int = 1,
    ) -> Outcome:
        """Drive one report and return what was observed.

        ``on_dispatch`` is called immediately before the submit click. It must
        write the intent to the durable checkpoint before returning, because
        everything after that point is a race we would rather lose than win.
        """
        wizard = ReportWizard()
        await self.driver.goto_profile(target.handle)

        for _ in range(wizard.max_observations):
            if wizard.budget_exhausted():
                break
            state = wizard.observe(await self._read())
            log.debug("browser %s: %s", target.escaped(), state.value)

            if state in (WizardState.BLOCKED, WizardState.GONE):
                return await self._without_dispatch(
                    target,
                    wizard,
                    self._blocked(target, wizard, account_ref, lease, attempt),
                    account_ref,
                    lease,
                    attempt,
                )
            if state is WizardState.CONFIRMED:
                # A confirmation with no click is not a report we filed. It is
                # leftover UI from an earlier attempt in this same context, and
                # recording it as a success would be inventing a result.
                return await self._without_dispatch(
                    target,
                    wizard,
                    self._not_reported(
                        target,
                        wizard,
                        "a confirmation was already on the page before any "
                        "click; that is leftover UI, not a report this attempt "
                        "submitted",
                        account_ref,
                        lease,
                        attempt,
                    ),
                    account_ref,
                    lease,
                    attempt,
                )

            action = wizard.action()
            if action is Action.NONE:
                break
            if action is Action.WAIT:
                await self._settle()
                continue
            if action is Action.OPEN_MENU:
                await self.driver.open_menu()
                wizard.note_click()
            elif action is Action.PICK_MENU_ITEM:
                await self.driver.pick_menu_item()
                wizard.note_click()
            elif action is Action.CHOOSE_CATEGORY:
                if not await self._choose(wizard, target):
                    return await self._without_dispatch(
                        target,
                        wizard,
                        self._not_reported(
                            target,
                            wizard,
                            f"category selection refused: {wizard.category_failure}",
                            account_ref,
                            lease,
                            attempt,
                        ),
                        account_ref,
                        lease,
                        attempt,
                    )
            elif action is Action.SUBMIT:
                # The boundary, written before the click and never after.
                on_dispatch()
                wizard.declare_dispatch()
                await self.driver.click_submit()
                wizard.note_click()
                # Hand off. Every reading from here on belongs to _finish, which
                # knows about the dispatch boundary; this loop does not, and
                # staying in it would read the confirmation the click just
                # produced as leftover UI from a previous attempt and answer
                # CHANNEL_FAILED for a report that went out. The leftover-UI
                # check above is only true before a click, which is exactly the
                # condition this break guarantees.
                break
            await self._settle()

        return await self._finish(target, wizard, account_ref, lease, attempt)

    async def _finish(
        self,
        target: Target,
        wizard: ReportWizard,
        account_ref: str | None,
        lease: ProxyLease | None,
        attempt: int,
    ) -> Outcome:
        """Post-submit readings, then a verdict.

        The first reading that carries a captured submit ends the wait for the
        network. The rest exist to prove the confirmation *persisted*, which is
        what defeats the toast race: Instagram shows a transient "Report
        submitted" toast that satisfies the confirmation anchor and then
        vanishes, and a single reading cannot tell that apart from the wizard
        having genuinely finished.
        """
        if not wizard.dispatched:
            return await self._without_dispatch(
                target,
                wizard,
                self._not_reported(
                    target,
                    wizard,
                    f"the wizard never reached a submit affordance "
                    f"({wizard.summary()})",
                    account_ref,
                    lease,
                    attempt,
                ),
                account_ref,
                lease,
                attempt,
            )

        for poll in range(self.confirmation_polls):
            observation = await self._read(
                submit=self.capture.select(), at=float(poll)
            )
            wizard.observe(observation)
            log.debug(
                "browser %s post-submit reading %d: %s",
                target.escaped(),
                poll,
                wizard.state.value,
            )
            # A blocker seen here is deliberately NOT routed to self._blocked.
            # The report is already in the air, so answering CHANNEL_FAILED or
            # QUARANTINED from this method would record dispatched_at=None for a
            # report that was sent, which is precisely the false-negative this
            # tool exists to stop. Instead the reading flows into evidence(),
            # where classify_browser raises AccountChallenged -- raised rather
            # than returned, so the caller quarantines the account instead of
            # falling through the ladder onto a target we may have reported.
            if wizard.submit is not None and wizard.confirmation_stable:
                break
            if poll + 1 < self.confirmation_polls:
                await self._settle()

        evidence = wizard.evidence()
        try:
            terminal = classify_browser(evidence, self.confirm_marker)
        except InstaReportError:
            # classify_browser raises rather than returning when a wall appeared
            # after the click. That is the right control flow -- the caller has
            # to quarantine the account, and a returned state would let the
            # ladder re-report a target that may already be filed. But it means
            # the most interesting failure of all used to leave nothing behind,
            # because the bundle is written after the verdict and here there
            # never was one. Write it on the way out, then re-raise.
            await self._capture_raised(target, wizard, account_ref, lease, attempt)
            raise
        detail = wizard.summary()
        if wizard.post_boundary_ready:
            detail += (
                "; a second submit affordance appeared after dispatch and was "
                "not clicked, because a two-stage confirm and a submitted "
                "report are indistinguishable from here"
            )
        outcome = Outcome(
            terminal=terminal,
            target_ref=target.key,
            channel=CHANNEL,
            account_ref=account_ref,
            lease_id=lease.lease_id if lease else None,
            attempt=attempt,
            resolved_user_id=target.user_id,
            dispatched_at=utc_now(),
            finished_at=utc_now(),
            detail=detail,
        )
        if terminal is not TerminalState.SUBMITTED_ACKED:
            await self._capture_evidence(
                target, wizard, outcome, account_ref, lease, attempt
            )
        return outcome

    # -- helpers ----------------------------------------------------------

    async def _read(
        self, *, submit: SubmitRequest | None = None, at: float = 0.0
    ) -> Observation:
        async def probe(
            name: str, *, require_enabled: bool, timeout_ms: int | None
        ) -> str | None:
            return await self.driver.read(name, require_enabled=require_enabled)

        return await observe(
            self.anchors,
            probe,
            category_probe=self.driver.selected_category,
            submit=submit,
            at=at,
        )

    async def _choose(self, wizard: ReportWizard, target: Target) -> bool:
        offered = await self.driver.offered_categories()
        category, reason = self.policy.choose(offered, detail=target.detail or "")
        if category is None:
            wizard.note_category_failure(reason)
            return False
        await self.driver.choose_category(category)
        wizard.note_click()
        return True

    async def _settle(self) -> None:
        if self.poll_interval:
            await asyncio.sleep(self.poll_interval)

    async def aclose(self) -> None:
        """Release the browser. Part of the runner's channel protocol.

        Delegates straight to the driver rather than adding a second shutdown
        path: there is already exactly one place that answers "did we actually
        stop the browser", and duplicating it here is how the two drift apart
        and one of them starts leaking a process.
        """
        await self.driver.close()

    async def _capture_evidence(
        self,
        target: Target,
        wizard: ReportWizard,
        outcome: Outcome,
        account_ref: str | None,
        lease: ProxyLease | None,
        attempt: int,
    ) -> None:
        """Write the bundle. Never raises into the report path.

        A failure to record why a report failed must not become the reason the
        run stops, so every error here is logged and swallowed and the ledger
        entry is still written. The alternative is an operator whose run died
        with a truncated report count because a screenshot could not be taken.
        """
        if self.artifacts is None:
            return
        try:
            submit = wizard.submit
            # Pre-dispatch there is no "after" to speak of, so the anchors that
            # were on the page are the hit set. Reporting them as misses would
            # turn every quarantine into a bundle claiming it saw nothing, which
            # is the opposite of what a bundle is for.
            hit = wizard.post_seen if wizard.dispatched else wizard.seen
            context = FailureContext(
                target_key=target.key,
                target_display=target.escaped(),
                channel=CHANNEL,
                terminal=outcome.terminal,
                detail=outcome.detail,
                anchors_hit=tuple(sorted(hit)),
                anchors_missed=tuple(sorted(wizard.seen - hit)),
                submit_status=submit.status if submit else None,
                submit_body_excerpt=submit.body[:2000] if submit else "",
                submit_url=submit.url if submit else "",
                account_display=account_ref or "",
                proxy_origin=lease.endpoint.origin if lease else "",
                attempt=attempt,
            )
            self.artifacts.capture(context, html=await self.driver.html())
        except Exception:  # noqa: BLE001
            log.error(
                "evidence bundle for %s could not be written; the report result "
                "is unaffected but the reason exists only in this log",
                target.escaped(),
                exc_info=True,
            )

    async def _capture_raised(
        self,
        target: Target,
        wizard: ReportWizard,
        account_ref: str | None,
        lease: ProxyLease | None,
        attempt: int,
    ) -> None:
        """Bundle a failure that raised instead of returning.

        The state recorded is QUARANTINED because every exception
        ``classify_browser`` raises on this path is an account-side one; the
        precise reason is the exception the caller is about to see, and the
        bundle's job is to carry what the page looked like when it happened.
        """
        placeholder = Outcome(
            terminal=TerminalState.QUARANTINED,
            target_ref=target.key,
            channel=CHANNEL,
            account_ref=account_ref,
            lease_id=lease.lease_id if lease else None,
            attempt=attempt,
            resolved_user_id=target.user_id,
            dispatched_at=utc_now(),
            finished_at=utc_now(),
            detail=(
                "raised out of the classifier after dispatch; the page at that "
                f"point was {wizard.summary()}"
            ),
        )
        await self._capture_evidence(
            target, wizard, placeholder, account_ref, lease, attempt
        )

    async def _without_dispatch(
        self,
        target: Target,
        wizard: ReportWizard,
        outcome: Outcome,
        account_ref: str | None,
        lease: ProxyLease | None,
        attempt: int,
    ) -> Outcome:
        """Bundle a failure that happened before anything was sent.

        These are the failures an operator most needs to see, not least: a
        channel that quietly gave up writes one ledger line, and without the
        page there is no way to tell a selector that stopped matching from a
        dialog Instagram changed. It costs nothing on the success path because
        the success path never calls this.
        """
        await self._capture_evidence(
            target, wizard, outcome, account_ref, lease, attempt
        )
        return outcome

    # -- the shapes of not-a-report ---------------------------------------

    def _not_reported(
        self,
        target: Target,
        wizard: ReportWizard,
        detail: str,
        account_ref: str | None,
        lease: ProxyLease | None,
        attempt: int,
    ) -> Outcome:
        return Outcome(
            terminal=TerminalState.CHANNEL_FAILED,
            target_ref=target.key,
            channel=CHANNEL,
            account_ref=account_ref,
            lease_id=lease.lease_id if lease else None,
            attempt=attempt,
            resolved_user_id=target.user_id,
            dispatched_at=None,
            finished_at=utc_now(),
            detail=detail,
        )

    def _blocked(
        self,
        target: Target,
        wizard: ReportWizard,
        account_ref: str | None,
        lease: ProxyLease | None,
        attempt: int,
    ) -> Outcome:
        """A blocker, typed by *what* is blocked rather than merely that it is.

        Only ever reached pre-dispatch, which is what keeps ``dispatched_at``
        honest: everything here is a report that was not sent.

        A challenge ends the lease. A login wall means the session died and a
        human must re-authenticate. A 404 is about the target, and must not cost
        budget or push the ladder, because every channel will fail identically
        on a deleted account -- so it is NOT_REPORTABLE rather than a channel
        failure, and the runner will not try again anywhere.
        """
        hit = classify_blocked(wizard.seen)
        common = {
            "target_ref": target.key,
            "channel": CHANNEL,
            "account_ref": account_ref,
            "lease_id": lease.lease_id if lease else None,
            "attempt": attempt,
            "resolved_user_id": target.user_id,
            "dispatched_at": None,
            "finished_at": utc_now(),
        }
        if hit & TARGET_BLOCKERS:
            return Outcome(
                terminal=TerminalState.NOT_REPORTABLE,
                detail=f"target is not reportable: {sorted(hit)}",
                **common,
            )
        if "login_wall" in hit:
            return Outcome(
                terminal=TerminalState.QUARANTINED,
                error_class=SessionExpired.__name__,
                error_scope=ErrorScope.LEASE.value,
                detail="the session is no longer logged in; a human must "
                "re-authenticate before this account can report again",
                **common,
            )
        # challenge or rate_limited. Both mean the exit is unusable for now.
        return Outcome(
            terminal=TerminalState.QUARANTINED,
            detail=f"blocked before submit: {sorted(hit)}",
            **common,
        ).with_error(AccountChallenged(f"blocked before submit: {sorted(hit)}"))
