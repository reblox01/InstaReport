"""Targets as structured records (D8).

The original tool took a list of bare handles and reported each one. A handle is
the *least* of what a report needs, and treating it as the whole thing is what
made the original unable to answer questions it should have been able to answer:
which account this was, who supplied it, why it is being reported, whether it
has already been attempted, and whether the resolved user id matches what we
expected.

The record here is the minimum that makes a report reviewable afterwards:

```
  target        what was asked for: "somehandle"
  user_id       what Instagram resolved it to: "1234567890"
  category      what we chose from the live dialog: "Spam"
  detail        the per-target note sent with it
  supplied_by   who put this in the list
  attempt       how many times this tool has tried
  last_terminal what happened last time
```

``user_id`` is the field that matters most and the one the original never had.
A handle is a *display name* and is reassignable; the numeric id is the account.
Recording both means an outcome can always be traced to the specific account it
was filed against, and means a handle that was reassigned between the list being
written and the run happening is visible afterwards instead of silently
mis-filed.

Confusable handles (F8) are recorded per target and flagged, because a handle
containing Cyrillic lookalikes resolves to a *different real account* -- that is
a harm vector, not a miss.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .outcomes import TerminalState

__all__ = [
    "Target",
    "TargetList",
    "TargetProblem",
    "parse_targets",
    "load_targets",
    "read_handles",
    "render_problems",
    "RESERVED",
]

log = logging.getLogger(__name__)

#: Instagram's own rule. Not enforced here beyond validation, but recorded so a
#: target that Instagram would refuse is caught before dispatch rather than after.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

#: Characters Instagram does not accept in a handle. Rejected at load rather
#: than at dispatch, so a typo in a list is a load error and not a run that
#: silently does less than asked.
_ILLEGAL = set(" \t\r\n/\\?#@")

#: Reserved words. Instagram refuses these outright, and the refusal is a 404 on
#: the profile rather than an error, which is indistinguishable from a deleted
#: account unless it is caught before dispatch.
RESERVED = frozenset(
    {
        "about", "accounts", "admin", "api", "business", "challenge", "create",
        "developer", "direct", "directory", "download", "edit", "explore",
        "favicon", "feed", "graphql", "help", "home", "i", "instagram", "jobs",
        "legal", "login", "logout", "me", "media", "new", "oauth", "p", "pages",
        "policies", "privacy", "pulse", "reel", "reels", "s", "session",
        "settings", "shopping", "signin", "signup", "status", "stories", "support",
        "terms", "u", "users", "web", "www",
    }
)


class TargetProblem(Exception):
    """A target list could not be used, with every problem reported at once.

    All problems rather than the first, because an operator fixing a list of 400
    handles one error message per run is a bad afternoon. The whole diagnosis is
    in one message, grouped by kind, with line numbers.
    """


@dataclass
class Target:
    """One account to be reported, and everything known about the attempt."""

    #: The handle as supplied. Kept verbatim -- never case-folded, never
    #: normalised -- because it is what the operator typed and what a reviewer
    #: needs to see. Comparison against a resolved profile is done separately
    #: and case-insensitively; confusables are *not* equal.
    handle: str

    #: The numeric id Instagram resolves the handle to. Filled in during
    #: resolution, before dispatch. This is the account, not the name.
    user_id: str | None = None

    #: The category as it appeared in the live dialog. Never one of a hardcoded
    #: table -- the original's 12-entry REPORT_REASONS is gone, so a category
    #: Instagram adds is selectable and one it removes cannot be filed for.
    category: str | None = None

    #: The free-text note sent with the report, if any.
    detail: str | None = None

    #: Who put this in the list. Operational only, but it is the difference
    #: between "we reported 400 accounts" and "we reported 400 accounts that
    #: came from the moderator's dump", and those warrant different responses.
    supplied_by: str | None = None

    #: An operator note. Never sent to Instagram.
    note: str | None = None

    #: Attempts already made, from the checkpoint ledger. Not persisted in the
    #: target itself -- the ledger is the authority, and two sources of truth for
    #: "have we tried this" is how a target gets reported twice.
    attempt: int = 0
    last_terminal: TerminalState | None = None
    last_channel: str | None = None
    last_detail: str | None = None

    #: The configured lookalike table (from the anchor file). Not target data, so
    #: it is excluded from comparison, from ``repr``, and from the serialised
    #: record -- two targets that differ only in which anchor file was loaded
    #: are the same target, and a record must not carry config with it.
    _confusable_table: dict[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self.handle = self.handle.strip()
        if not self.handle:
            raise ValueError("target handle may not be empty")

    # -- identity -------------------------------------------------------

    @property
    def key(self) -> str:
        """Identity for ledger and de-duplication.

        Case-folded, because Instagram handles are case-insensitive and
        ``SomeUser`` and ``someuser`` are the same account. Deliberately *not*
        Unicode-normalised: ``аctor`` (Cyrillic a) and ``actor`` are different
        accounts, and folding them together here would be exactly the F8 conflation
        the design refuses to make.
        """
        return self.handle.casefold()

    @property
    def codepoints(self) -> str:
        """The handle as a codepoint string, for reviewing confusables.

        ``"\\u0430ctor"`` rather than ``"actor"``, so the difference is visible
        in a log even though the two render identically in a terminal.
        """
        return "".join(f"\\u{ord(c):04x}" if ord(c) > 127 else c for c in self.handle)

    def confusables(self) -> list[str]:
        """Named lookalike characters present in this handle.

        Uses the configured table rather than a hardcoded set, so an operator who
        hits a lookalike this file has never heard of can add it without a code
        change -- the same reasoning as D7.
        """
        return [
            f"{entry.name} ({entry.codepoint})"
            for char in self.handle
            for entry in self._confusable_table.values()
            if entry.matches(char)
        ]

    def escaped(self) -> str:
        """The handle with every non-ASCII codepoint written out.

        Used in every message that names a target, for two reasons that happen
        to be the same fix. A Windows console defaults to cp1252, so printing a
        handle containing U+0430 raises UnicodeEncodeError and takes the process
        down -- and the handle that crashes the console is precisely the one a
        reviewer needs to read, because ``аctor`` and ``actor`` are different
        accounts that look identical. Escaping makes the message safe to print
        *and* makes the difference visible, which a raw handle never manages.
        """
        return "".join(
            f"\\u{ord(c):04x}" if ord(c) > 127 else c for c in self.handle
        )

    def non_ascii(self) -> list[str]:
        """Non-ASCII characters in the handle, with names.

        A non-ASCII character makes the handle *invalid* -- Instagram handles are
        ASCII-only, so there is no real account this could legitimately be. It
        is not, however, a reason to refuse the operator's work: the character
        is either a display name pasted by mistake or a lookalike, and in both
        cases the fix is the same and the operator is the only one who can make
        it. ``validate`` turns this into "did you mean <the ASCII form>?" for
        exactly that reason.
        """
        return [
            f"U+{ord(c):04X} {unicodedata.name(c, '?')}"
            for c in self.handle
            if ord(c) > 127
        ]

    def ascii_form(self) -> str:
        """This handle with every lookalike replaced by its ASCII twin.

        Only substitutes codepoints present in the confusable table, so it never
        invents a handle: a name in Greek with no ASCII counterpart comes back
        with that part left alone, and the caller can see there is no plausible
        reading to offer.
        """
        out: list[str] = []
        for char in self.handle:
            entry = self._confusable_for(char)
            out.append((entry.ascii_twin or char) if entry else char)
        return "".join(out)

    def _confusable_for(self, char: str) -> Any:
        for entry in self._confusable_table.values():
            if entry.matches(char):
                return entry
        return None

    def _confusables(self) -> list[str]:
        return self.confusables()

    # -- validation -----------------------------------------------------

    def validate(self) -> list[str]:
        """Everything wrong with this target, as human-readable strings.

        Returns rather than raises, so one target's problems do not stop the rest
        of the list from being diagnosed. ``TargetProblem`` is raised by the
        caller once it has collected them all.
        """
        problems: list[str] = []
        if not _HANDLE_RE.match(self.handle):
            illegal = sorted(set(self.handle) & _ILLEGAL)
            if illegal:
                problems.append(
                    f"contains characters Instagram does not accept: "
                    f"{''.join(illegal)!r}"
                )
            else:
                # The escaped form, not the raw handle: see ``escaped``. A
                # non-ASCII handle fails this check precisely when the codepoint
                # matters, and printing it raw is both unsafe on a default
                # Windows console and unreadable in the cases that need review.
                problems.append(
                    f"is not a valid handle: {self.escaped()!r} (expected 1-30 of "
                    "A-Z a-z 0-9 . _)"
                )
            # Now the actionable version, which is the one that matters. A
            # generic "invalid handle" leaves the operator to work out whether
            # they meant the Latin one; naming the ASCII reading saves them from
            # filing a report against the wrong account.
            confusables = self._confusables()
            if confusables:
                twin = self.ascii_form()
                hint = (
                    f"; if you meant {twin!r}, use that instead"
                    if twin != self.handle
                    else ""
                )
                problems.append(
                    f"contains lookalike characters ({', '.join(confusables)}) "
                    f"which render identically to ASCII but are different "
                    f"codepoints, so it resolves to a different account{hint}"
                )
        if self.handle.casefold() in RESERVED:
            problems.append(
                f"{self.escaped()!r} is a reserved word; Instagram serves a 404 for "
                "it, which is indistinguishable from a deleted account"
            )
        if self.user_id is not None and not self.user_id.isdigit():
            problems.append(f"user_id {self.user_id!r} is not numeric")
        return problems

    @property
    def is_resolved(self) -> bool:
        return self.user_id is not None

    @property
    def was_attempted(self) -> bool:
        return self.attempt > 0

    @property
    def is_exhausted(self) -> bool:
        """An UNKNOWN is terminal for this target, permanently.

        Not retried, not fallen through to another channel. A report whose fate
        is unknown might have been filed; re-sending it is the one action that
        can produce a duplicate against a real account, which is a harm.
        ``resume`` is how an operator overrides this, deliberately and visibly.
        """
        return self.last_terminal is TerminalState.UNKNOWN

    # -- serialisation --------------------------------------------------

    def to_record(self) -> dict[str, Any]:
        data = {k: v for k, v in asdict(self).items() if k != "_confusable_table"}
        data["last_terminal"] = (
            self.last_terminal.value if self.last_terminal else None
        )
        data["key"] = self.key
        return data

    @classmethod
    def from_record(
        cls, data: dict[str, Any], confusables: dict[str, Any] | None = None
    ) -> "Target":
        terminal = data.get("last_terminal")
        return cls(
            handle=data["handle"],
            user_id=data.get("user_id"),
            category=data.get("category"),
            detail=data.get("detail"),
            supplied_by=data.get("supplied_by"),
            note=data.get("note"),
            attempt=int(data.get("attempt", 0)),
            last_terminal=TerminalState(terminal) if terminal else None,
            last_channel=data.get("last_channel"),
            last_detail=data.get("last_detail"),
            _confusable_table=confusables or {},
        )

    def __str__(self) -> str:
        uid = f" -> {self.user_id}" if self.user_id else " (unresolved)"
        return f"{self.handle}{uid}"


@dataclass
class TargetList:
    """A loaded, validated set of targets."""

    targets: list[Target] = field(default_factory=list)
    source: str = ""
    #: Problems found while loading. Non-empty means the list is unusable, but
    #: they are held rather than raised so a caller can report them all at once.
    problems: list[str] = field(default_factory=list)

    def __iter__(self) -> Iterator[Target]:
        return iter(self.targets)

    def __len__(self) -> int:
        return len(self.targets)

    def __bool__(self) -> bool:
        return bool(self.targets)

    @property
    def usable(self) -> bool:
        return bool(self.targets) and not self.problems

    def keys(self) -> set[str]:
        return {t.key for t in self.targets}

    def get(self, key: str) -> Target | None:
        folded = key.casefold()
        for target in self.targets:
            if target.key == folded:
                return target
        return None

    def unresolved(self) -> list[Target]:
        return [t for t in self.targets if not t.is_resolved]

    def attempted(self) -> list[Target]:
        return [t for t in self.targets if t.was_attempted]

    def exhausted(self) -> list[Target]:
        """Targets whose fate is unknown. Never re-sent without human review."""
        return [t for t in self.targets if t.is_exhausted]

    def pending(self) -> list[Target]:
        """Targets still to do: never attempted, or attempted and safely failed."""
        return [
            t
            for t in self.targets
            if not t.was_attempted
            or t.last_terminal in _RETRYABLE_TERMINALS
        ]

    def self_reporting(self, account_usernames: Iterable[str]) -> list[Target]:
        """Targets that are one of the reporting accounts (F-harm).

        Cross-checked before filing, because an account reporting itself is an
        instant self-lock: Instagram will restrict the reporting account, and
        the run loses the identity that filed it. A handful of entries in a
        400-handle list being the operator's own accounts is a mundane mistake,
        not an attack, and it is caught here rather than by Instagram.
        """
        owned = {u.strip().casefold().lstrip("@") for u in account_usernames}
        return [t for t in self.targets if t.key in owned]

    def require_usable(self) -> None:
        if self.problems:
            raise TargetProblem(render_problems(self.problems))

    def to_records(self) -> list[dict[str, Any]]:
        return [t.to_record() for t in self.targets]

    @classmethod
    def from_records(
        cls,
        records: Sequence[dict[str, Any]],
        source: str = "",
        confusables: dict[str, Any] | None = None,
    ) -> "TargetList":
        targets: list[Target] = []
        seen: dict[str, int] = {}
        problems: list[str] = []
        for position, record in enumerate(records, start=1):
            try:
                target = Target.from_record(record, confusables)
            except (KeyError, ValueError) as exc:
                problems.append(f"entry {position}: unusable ({exc})")
                continue
            for problem in target.validate():
                problems.append(f"{target.escaped()}: {problem}")
            if target.key in seen:
                problems.append(
                    f"{target.escaped()}: duplicate of entry {seen[target.key]} "
                    "(handles are case-insensitive, so these are the same account "
                    "and reporting both wastes an attempt)"
                )
                continue
            seen[target.key] = position
            targets.append(target)
        return cls(targets=targets, source=source, problems=problems)


#: Terminal states a target may be retried from. Deliberately excludes UNKNOWN
#: (see ``Target.is_exhausted``) and QUARANTINED (the target is fine; the
#: account was sidelined, so the target is retried with a *different* identity).
_RETRYABLE_TERMINALS = frozenset(
    {
        TerminalState.CHANNEL_FAILED,
        TerminalState.NOT_REPORTABLE,
        TerminalState.SUBMITTED_UNCONFIRMED,
    }
)


def read_handles(path: Path) -> list[str]:
    """Read a plain list of handles, one per line.

    Kept as a supported input because a 400-line moderation dump is a text file
    and forcing it through JSON adds a conversion step with no benefit. Comments
    and blanks are skipped, because a dump exported from a wiki will have both.
    """
    handles: list[str] = []
    for lineno, raw in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        handle = line.lstrip("@").strip()
        if "/" in handle:
            # Accept a full profile URL, because that is what a browser gives
            # you and pasting the whole URL is the obvious thing to do.
            handle = handle.rstrip("/").rsplit("/", 1)[-1]
        if not handle:
            log.warning("%s:%d: no handle in %r", path, lineno, raw.strip())
            continue
        handles.append(handle)
    return handles


def parse_targets(
    data: Any,
    *,
    source: str = "",
    confusables: dict[str, Any] | None = None,
) -> TargetList:
    """Accept either a list of handles or a list of target records.

    Both, because both are things an operator has: a quick list typed into a
    terminal, and a reviewed file with categories and provenance attached. The
    structured form is a superset, so there is one code path for the full shape
    and a narrow adapter for the bare one.
    """
    if isinstance(data, dict):
        supplied_by = data.get("supplied_by")
        default_detail = data.get("detail")
        items = data.get("targets", data.get("handles", []))
        if not isinstance(items, list):
            return TargetList(
                problems=[f"'targets' must be a list, got {type(items).__name__}"]
            )
        records: list[dict[str, Any]] = []
        for entry in items:
            if isinstance(entry, str):
                records.append(
                    {"handle": entry, "supplied_by": supplied_by, "detail": default_detail}
                )
            elif isinstance(entry, dict):
                record = dict(entry)
                record.setdefault("supplied_by", supplied_by)
                if default_detail and not record.get("detail"):
                    record["detail"] = default_detail
                records.append(record)
            else:
                records.append({"handle": str(entry)})
        return TargetList.from_records(records, source=source, confusables=confusables)

    if isinstance(data, list):
        # A list may be bare handles or full records. Round-tripping records
        # through this function is an obvious thing to do -- it is how a resumed
        # run reloads its list -- so a dict entry is honoured rather than
        # stringified. Stringifying it would produce a handle of the form
        # "{'handle': 'x', ...}", which fails validation with a message that
        # describes the wrong problem entirely.
        records = [
            item if isinstance(item, dict) else {"handle": str(item)}
            for item in data
        ]
        return TargetList.from_records(
            records, source=source, confusables=confusables
        )

    return TargetList(
        problems=[
            f"target list must be a list or an object, got {type(data).__name__}"
        ]
    )


def load_targets(path: Path, confusables: dict[str, Any] | None = None) -> TargetList:
    """Load from ``.json``, ``.toml`` or a plain handle list, by extension.

    Extension-based rather than content-sniffing: a ``.txt`` full of JSON is
    almost certainly a mistake worth surfacing, not a format to accommodate.
    """
    suffix = path.suffix.casefold()
    text = path.read_text(encoding="utf-8", errors="replace")

    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return TargetList(
                source=str(path),
                problems=[f"{path}: invalid JSON -- {exc}"],
            )
    elif suffix == ".toml":
        try:
            data = tomllib_loads(text)
        except Exception as exc:  # noqa: BLE001 - any parse failure is the same report
            return TargetList(source=str(path), problems=[f"{path}: invalid TOML -- {exc}"])
    else:
        return TargetList.from_records(
            [{"handle": h} for h in read_handles(path)],
            source=str(path),
            confusables=confusables,
        )

    return parse_targets(data, source=str(path), confusables=confusables)


def tomllib_loads(text: str) -> Any:
    import tomllib

    return tomllib.loads(text)


def render_problems(problems: Sequence[str]) -> str:
    """All problems, grouped by kind, with the count in the headline.

    An operator with 400 handles and 6 bad ones should see 6 lines, not discover
    them across 6 runs.
    """
    if not problems:
        return "target list is usable"
    lines = [f"{len(problems)} problem(s) in the target list:"]
    lines.extend(f"  - {problem}" for problem in problems)
    lines.append("")
    lines.append(
        "Nothing was reported. Fix the list and re-run; no state was changed."
    )
    return "\n".join(lines)
