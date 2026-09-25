"""The result contract.

This module exists because ``igban.py`` had no such thing. It checked
``status == 200``, called ``.json()`` on an HTML body, caught the resulting
exception with a bare ``except:``, printed "reported successfully", and returned
``True``. Every run reported total success having sent nothing.

Two rules follow from that failure and are enforced by tests here.

**1. There is no positive path without evidence.** ``SUBMITTED_ACKED`` requires
*both* a readable network response and a DOM state transition. Confirmation
appearing in the DOM while the network said otherwise is ``UNKNOWN``, never
``ACKED`` -- Instagram renders success optimistically to reporters it does not
trust, which is precisely the shape a UI-only classifier cannot see.

**2. After dispatch, nothing retries and nothing falls through.** The dispatch
boundary is a checkpoint write, not an in-memory flag. Before it, any failure
may move to the next channel. After it, every terminal state is terminal.
``_POST_DISPATCH_TERMINAL`` is the invariant; ``test_dispatch_boundary.py``
holds it.

A note on vocabulary, because it is load-bearing: Instagram gives a reporter no
way to verify that a report was recorded, so nothing here claims an account was
reported. These states describe the *request*, never the *account*.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

__all__ = [
    "TerminalState",
    "NetworkVerdict",
    "BrowserEvidence",
    "Outcome",
    "classify_network",
    "classify_browser",
    "classify_api",
    "utc_now",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TerminalState(str, Enum):
    """Where one report attempt ended up."""

    #: Dispatched, and both the network and the DOM confirm it.
    SUBMITTED_ACKED = "submitted_acked"
    #: Dispatched, and the response is readable but not a success. A clean
    #: rejection -- quota, policy, duplicate -- not a lost request.
    SUBMITTED_UNCONFIRMED = "submitted_unconfirmed"
    #: Dispatched, response uninterpretable. We do not know whether it landed.
    UNKNOWN = "unknown"
    #: Target gone, deleted, or unresolvable. No budget spent, no fallthrough.
    NOT_REPORTABLE = "not_reportable"
    #: This channel could not attempt it. The ladder may continue.
    CHANNEL_FAILED = "channel_failed"
    #: Not attempted; the lease is out of service.
    QUARANTINED = "quarantined"

    @property
    def stops_ladder(self) -> bool:
        """Whether this ends the report attempt rather than the channel attempt."""
        return self is not TerminalState.CHANNEL_FAILED

    @property
    def needs_human_review(self) -> bool:
        """Whether a person must check this one out of band.

        True for exactly the two states where we cannot tell what Instagram did.
        """
        return self in {TerminalState.SUBMITTED_UNCONFIRMED, TerminalState.UNKNOWN}

    @property
    def counts_against_budget(self) -> bool:
        """Whether this outcome spends part of the account's report allowance.

        A report that was never attempted -- gone target, quarantined lease --
        costs nothing. Once dispatched, it has cost a report whatever the
        response said.
        """
        return self not in {
            TerminalState.NOT_REPORTABLE,
            TerminalState.QUARANTINED,
            TerminalState.CHANNEL_FAILED,
        }


#: Post-dispatch, every state is terminal. Asserted in tests; the dict exists so
#: the invariant is data rather than a comment.
_POST_DISPATCH_TERMINAL = frozenset(
    {
        TerminalState.SUBMITTED_ACKED,
        TerminalState.SUBMITTED_UNCONFIRMED,
        TerminalState.UNKNOWN,
    }
)


class NetworkVerdict(str, Enum):
    OK = "ok"
    REJECTED = "rejected"
    UNREADABLE = "unreadable"
    NONE = "none"


def classify_network(
    *,
    status: int | None,
    body: str | None,
    content_type: str | None,
    timed_out: bool = False,
) -> NetworkVerdict:
    """Read a submit response without ever guessing.

    An unrecognised body is ``UNREADABLE``, never ``OK``. Assuming a 200 means
    success is the exact bug being fixed.
    """
    if timed_out:
        return NetworkVerdict.UNREADABLE
    if status is None:
        return NetworkVerdict.NONE

    if not body or not body.strip():
        return NetworkVerdict.UNREADABLE

    looks_json = bool(content_type and "json" in content_type.lower())
    if not looks_json:
        # A 200 carrying an HTML login shell is the shape that broke the last
        # version of this tool. Never readable as success.
        return NetworkVerdict.UNREADABLE

    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return NetworkVerdict.UNREADABLE
    if not isinstance(payload, dict):
        return NetworkVerdict.UNREADABLE

    explicit_fail = payload.get("status") == "fail" or bool(payload.get("message"))
    if explicit_fail:
        return NetworkVerdict.REJECTED

    if not (200 <= status < 300):
        return NetworkVerdict.REJECTED

    # Success requires an explicit ``status: "ok"``. A 2xx JSON body with no
    # status field is not assumed to be a success -- that assumption is the bug
    # this whole module exists to remove, and it would happily accept a
    # response shape Instagram has not sent us yet.
    if payload.get("status") == "ok":
        return NetworkVerdict.OK
    return NetworkVerdict.REJECTED


@dataclass(frozen=True)
class BrowserEvidence:
    """Everything one browser submit attempt actually observed.

    Kept as a flat record of observations rather than a result so the classifier
    stays a pure function and the awkward cases are constructible in tests.
    """

    dispatched: bool
    #: Terminal-state anchors present BEFORE submit. If confirmation text is
    #: already here, matching it after submit proves nothing.
    pre_submit_anchors: frozenset[str] = frozenset()
    post_submit_anchors: frozenset[str] = frozenset()
    #: Is the submit affordance still on the page? It should be gone.
    submit_affordance_gone: bool | None = None
    #: Inline validation messages surfaced by the form.
    validation_errors: tuple[str, ...] = ()
    network_status: int | None = None
    network_body: str | None = None
    network_content_type: str | None = None
    timed_out: bool = False
    #: A challenge or logged-out wall was hit. Not an HTTP condition.
    interstitial: bool = False

    @property
    def dom_advanced(self) -> bool:
        """A confirmation anchor appeared that was not there before.

        A substring of a pre-submit explanation, a button label, or a
        mid-wizard step all fail this test, which is the point.
        """
        appeared = self.post_submit_anchors - self.pre_submit_anchors
        return bool(appeared)


def classify_browser(evidence: BrowserEvidence, confirm_anchor: str) -> TerminalState:
    """Turn observations into a terminal state. Network-first, DOM corroborates.

    ``ACKED`` requires both signals. DOM-only confirmation is ``UNKNOWN``,
    because that is the optimistic-UI shape and it is the one a UI-only
    classifier cannot catch.
    """
    if not evidence.dispatched:
        # Pre-dispatch: nothing was sent, so the ladder is free to move on.
        return TerminalState.CHANNEL_FAILED

    if evidence.interstitial:
        # Raised, not returned: a challenge is a lease-scoped fatal, and the
        # caller needs to quarantine rather than continue.
        from .errors import AccountChallenged

        raise AccountChallenged(
            "challenge or logged-out interstitial reached during submit"
        )

    verdict = classify_network(
        status=evidence.network_status,
        body=evidence.network_body,
        content_type=evidence.network_content_type,
        timed_out=evidence.timed_out,
    )

    if evidence.validation_errors:
        # Readable rejection that the form itself reported.
        return TerminalState.SUBMITTED_UNCONFIRMED

    if verdict is NetworkVerdict.OK:
        confirmed = (
            evidence.dom_advanced
            and confirm_anchor in evidence.post_submit_anchors
            and confirm_anchor not in evidence.pre_submit_anchors
            and evidence.submit_affordance_gone is True
        )
        if confirmed:
            return TerminalState.SUBMITTED_ACKED
        # Sent, and Instagram says it was fine, but the UI did not advance past
        # the wizard. Something is wrong that we cannot name.
        return TerminalState.SUBMITTED_UNCONFIRMED

    if verdict is NetworkVerdict.REJECTED:
        return TerminalState.SUBMITTED_UNCONFIRMED

    # UNREADABLE, or NONE, or the DOM advanced while the network said nothing
    # usable. Both halves of the evidence disagree, or one is missing entirely.
    return TerminalState.UNKNOWN


def classify_api(
    *,
    status: int | None,
    body: str | None,
    content_type: str | None,
    timed_out: bool = False,
) -> TerminalState:
    """Classify a direct API submit. No DOM corroboration is available here.

    Kept separate from :func:`classify_browser` rather than folded in, because
    the API channel's evidence is strictly weaker and pretending otherwise
    would overstate its confidence.
    """
    if status is None and not timed_out:
        return TerminalState.CHANNEL_FAILED
    verdict = classify_network(
        status=status, body=body, content_type=content_type, timed_out=timed_out
    )
    if verdict is NetworkVerdict.NONE:
        return TerminalState.CHANNEL_FAILED
    if verdict is NetworkVerdict.OK:
        return TerminalState.SUBMITTED_ACKED
    if verdict is NetworkVerdict.REJECTED:
        return TerminalState.SUBMITTED_UNCONFIRMED
    return TerminalState.UNKNOWN


@dataclass(frozen=True)
class Outcome:
    """One report attempt's result. Appended to the checkpoint; never mutated."""

    terminal: TerminalState
    target_ref: str
    channel: str | None = None
    account_ref: str | None = None
    lease_id: str | None = None
    attempt: int = 1
    resolved_user_id: str | None = None
    dispatched_at: datetime | None = None
    finished_at: datetime | None = None
    error_class: str | None = None
    error_scope: str | None = None
    #: Paths to DOM dumps, screenshots, and response bodies kept as evidence.
    evidence_refs: tuple[str, ...] = ()
    detail: str = ""
    _extra: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # -- derived ---------------------------------------------------------

    @property
    def was_dispatched(self) -> bool:
        return self.dispatched_at is not None

    @property
    def stops_ladder(self) -> bool:
        return self.terminal.stops_ladder

    @property
    def needs_human_review(self) -> bool:
        return self.terminal.needs_human_review

    def with_error(self, error: Exception) -> "Outcome":
        scope = getattr(error, "scope", None)
        return replace(
            self,
            error_class=type(error).__name__,
            error_scope=getattr(scope, "value", None),
            detail=str(error) or self.detail,
        )

    def with_evidence(self, *refs: str) -> "Outcome":
        return replace(self, evidence_refs=self.evidence_refs + tuple(refs))

    # -- serialisation ----------------------------------------------------

    def to_record(self) -> dict[str, Any]:
        """Flatten to a JSONL-safe dict. Checkpoint format depends on this."""
        record: dict[str, Any] = {
            "terminal": self.terminal.value,
            "target_ref": self.target_ref,
            "channel": self.channel,
            "account_ref": self.account_ref,
            "lease_id": self.lease_id,
            "attempt": self.attempt,
            "resolved_user_id": self.resolved_user_id,
            "dispatched_at": _iso(self.dispatched_at),
            "finished_at": _iso(self.finished_at),
            "error_class": self.error_class,
            "error_scope": self.error_scope,
            "evidence_refs": list(self.evidence_refs),
            "detail": self.detail,
        }
        if self._extra:
            record["extra"] = dict(self._extra)
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "Outcome":
        return cls(
            terminal=TerminalState(record["terminal"]),
            target_ref=record["target_ref"],
            channel=record.get("channel"),
            account_ref=record.get("account_ref"),
            lease_id=record.get("lease_id"),
            attempt=int(record.get("attempt", 1)),
            resolved_user_id=record.get("resolved_user_id"),
            dispatched_at=_parse_iso(record.get("dispatched_at")),
            finished_at=_parse_iso(record.get("finished_at")),
            error_class=record.get("error_class"),
            error_scope=record.get("error_scope"),
            evidence_refs=tuple(record.get("evidence_refs") or ()),
            detail=record.get("detail", ""),
            _extra=record.get("extra") or {},
        )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
