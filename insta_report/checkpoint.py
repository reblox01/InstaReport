"""Durable run state.

The checkpoint is the only thing standing between a crash and a duplicate
report. It is append-only JSONL, one record per line, fsynced on every write,
because a report that was sent but not recorded is the worst outcome available:
the tool would report it as never attempted and send it again.

**The dispatch boundary is a write, not a flag.** An ``intent`` record is
appended and fsynced *before* the submit is triggered. That ordering is the
whole design:

    no intent on disk          -> nothing was attempted; safe to try
    intent, no outcome on disk -> may or may not have landed; UNKNOWN
    intent + outcome on disk   -> settled; never touch it again

The middle case is the one a naive implementation gets wrong. ``resume`` must
not retry it, and must not pretend it did not happen: Instagram gives a reporter
no way to confirm a report was recorded, so the honest state is UNKNOWN and a
human decides.

Append-only also means a torn final line -- the classic outcome of a power loss
mid-write -- degrades to a missing record rather than a corrupt file. The reader
drops a trailing unparseable line and says so, instead of raising and refusing
to start, because refusing to start is how a run gets restarted from scratch
and re-sends everything.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, TextIO

from .errors import CheckpointCorrupt, RunAborted
from .outcomes import Outcome, TerminalState, utc_now

__all__ = [
    "CheckpointError",
    "DoubleDispatch",
    "OrphanOutcome",
    "Intent",
    "SkipReason",
    "CheckpointState",
    "CheckpointStore",
]

log = logging.getLogger(__name__)

RECORD_INTENT = "intent"
RECORD_OUTCOME = "outcome"


class CheckpointError(RuntimeError):
    """Base for checkpoint integrity failures."""


class DoubleDispatch(CheckpointError):
    """A second outcome was written for a target that was already settled.

    Reaching this means the dispatch boundary failed somewhere upstream, and
    the safest response is to stop rather than append a third record.
    """


class OrphanOutcome(CheckpointError):
    """An outcome exists for a target that has no intent.

    Means the ordering guarantee was violated -- an outcome written before its
    intent, which should be impossible given the write order.
    """


class SkipReason(str, Enum):
    ALREADY_SETTLED = "already_settled"
    #: Dispatched, crashed before the outcome was written. Never auto-retried.
    DISPATCH_UNKNOWN = "dispatch_unknown"
    IN_PROGRESS = "in_progress"


@dataclass(frozen=True)
class Intent:
    """Written and fsynced *before* the submit is triggered."""

    run_id: str
    target_ref: str
    account_ref: str
    lease_id: str
    channel: str
    attempt: int = 1
    resolved_user_id: str | None = None
    at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.at is None:
            object.__setattr__(self, "at", utc_now())

    def to_record(self) -> dict[str, Any]:
        return {
            "kind": RECORD_INTENT,
            "run_id": self.run_id,
            "target_ref": self.target_ref,
            "account_ref": self.account_ref,
            "lease_id": self.lease_id,
            "channel": self.channel,
            "attempt": self.attempt,
            "resolved_user_id": self.resolved_user_id,
            "at": self.at.isoformat(),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "Intent":
        return cls(
            run_id=record["run_id"],
            target_ref=record["target_ref"],
            account_ref=record["account_ref"],
            lease_id=record["lease_id"],
            channel=record["channel"],
            attempt=int(record.get("attempt", 1)),
            resolved_user_id=record.get("resolved_user_id"),
            at=datetime.fromisoformat(record["at"]),
        )


class CheckpointState:
    """In-memory view of a run's progress, rebuilt by reading the log."""

    def __init__(self) -> None:
        self._intents: dict[str, Intent] = {}
        self._outcomes: dict[str, Outcome] = {}

    # -- queries ---------------------------------------------------------

    def has_intent(self, target_ref: str) -> bool:
        return target_ref in self._intents

    def outcome_for(self, target_ref: str) -> Outcome | None:
        return self._outcomes.get(target_ref)

    def skip_reason(self, target_ref: str) -> SkipReason | None:
        """Why this target must not be attempted, if it must not be."""
        if target_ref in self._outcomes:
            return SkipReason.ALREADY_SETTLED
        if target_ref in self._intents:
            return SkipReason.DISPATCH_UNKNOWN
        return None

    @property
    def dispatched(self) -> set[str]:
        """Targets with an intent -- everything that may have been sent."""
        return set(self._intents)

    @property
    def settled(self) -> set[str]:
        return set(self._outcomes)

    @property
    def pending(self) -> set[str]:
        """Dispatched with no recorded outcome. Always UNKNOWN, never retried."""
        return set(self._intents) - set(self._outcomes)

    def outcomes_needing_review(self) -> list[Outcome]:
        return [o for o in self._outcomes.values() if o.needs_human_review]

    def counts(self) -> dict[str, int]:
        tally = {state.value: 0 for state in TerminalState}
        for outcome in self._outcomes.values():
            tally[outcome.terminal.value] += 1
        return tally

    def __len__(self) -> int:
        return len(self._outcomes)

    def __contains__(self, target_ref: object) -> bool:
        return target_ref in self._outcomes

    # -- mutation (guarded) ----------------------------------------------

    def apply_intent(self, intent: Intent) -> None:
        if intent.target_ref in self._outcomes:
            raise DoubleDispatch(
                f"intent for already-settled target {intent.target_ref!r}; "
                "the target was settled and must not be attempted again"
            )
        self._intents[intent.target_ref] = intent

    def apply_outcome(self, outcome: Outcome) -> None:
        if outcome.target_ref not in self._intents:
            raise OrphanOutcome(
                f"outcome for {outcome.target_ref!r} with no intent record; "
                "the intent must be written and fsynced before dispatch"
            )
        if outcome.target_ref in self._outcomes:
            raise DoubleDispatch(
                f"second outcome for {outcome.target_ref!r} "
                f"(already {self._outcomes[outcome.target_ref].terminal.value})"
            )
        self._outcomes[outcome.target_ref] = outcome


def _fsync_dir(path: Path) -> None:
    """Persist a directory entry so a freshly created file survives a crash.

    No-op on Windows, where opening a directory handle for fsync is not
    supported; NTFS commits the directory entry with the file.
    """
    if sys.platform == "win32":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - filesystem-dependent
        pass
    finally:
        os.close(fd)


class CheckpointStore:
    """Append-only, fsync-per-record run log with a single-writer lock."""

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self._handle: TextIO | None = None
        # Loaded eagerly. Deferring to open() meant a caller who inspected
        # .state first saw an empty run and would conclude every target was
        # unattempted -- the most dangerous possible default here.
        self.state = self._load()

    # -- lifecycle --------------------------------------------------------

    def open(self) -> "CheckpointStore":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        _fsync_dir(self.path.parent)
        return self

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "CheckpointStore":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _require_open(self) -> Any:
        if self._handle is None:
            raise CheckpointError(f"checkpoint {self.path} is not open")
        return self._handle

    # -- reading ----------------------------------------------------------

    def _load(self) -> CheckpointState:
        state = CheckpointState()
        if not self.path.exists():
            return state

        # Read whole and split, rather than iterating the handle. Python
        # disables tell() on a text file mid-iteration, so locating "the last
        # line" through a live handle is not available -- and a crash-torn file
        # has no trailing newline, so any newline-based check would misread
        # exactly the case this tolerates.
        lines = self.path.read_text(encoding="utf-8").splitlines()
        last_content = max(
            (index for index, line in enumerate(lines) if line.strip()), default=-1
        )

        for index, raw in enumerate(lines):
            line = raw.strip()
            if not line:
                continue
            lineno = index + 1
            try:
                record = json.loads(line)
            except ValueError:
                # A torn final line is the expected shape of a crash mid-write.
                # Tolerate it, loudly, and keep going: refusing to start would
                # push the operator to resume from scratch and resend.
                if index == last_content:
                    log.warning(
                        "checkpoint %s: dropping unparseable final line %d "
                        "(torn write from an interrupted run)",
                        self.path,
                        lineno,
                    )
                    break
                raise CheckpointCorrupt(
                    f"{self.path}:{lineno} is not valid JSON. The checkpoint "
                    "cannot be trusted; continuing risks double-sends."
                ) from None
            self._apply(record, state)
        return state

    def _apply(self, record: Mapping[str, Any], state: CheckpointState) -> None:
        kind = record.get("kind")
        if kind == RECORD_INTENT:
            state.apply_intent(Intent.from_record(record))
        elif kind == RECORD_OUTCOME:
            state.apply_outcome(Outcome.from_record(record))
        else:
            raise CheckpointCorrupt(f"{self.path}: unknown record kind {kind!r}")

    # -- writing ----------------------------------------------------------

    def _append(self, record: Mapping[str, Any]) -> None:
        handle = self._require_open()
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        # flush() pushes the Python buffer out; fsync() is what makes it
        # durable. Without both, a report can be sent and the record lost in a
        # power failure -- the one outcome this whole module prevents.
        handle.flush()
        os.fsync(handle.fileno())

    def record_intent(self, intent: Intent) -> None:
        """Durably record the intent to report. Call before dispatching."""
        if intent.run_id != self.run_id:
            raise CheckpointError(
                f"intent run_id {intent.run_id!r} does not match store {self.run_id!r}"
            )
        self.state.apply_intent(intent)
        try:
            self._append(intent.to_record())
        except Exception:
            # Keep memory and disk in agreement: if it is not on disk, it did
            # not happen, and the runner must be free to treat it as unattempted.
            self.state._intents.pop(intent.target_ref, None)
            raise

    def record_outcome(self, outcome: Outcome) -> None:
        """Durably record a terminal outcome. The report is now settled."""
        self.state.apply_outcome(outcome)
        try:
            self._append({"kind": RECORD_OUTCOME, **outcome.to_record()})
        except Exception:
            self.state._outcomes.pop(outcome.target_ref, None)
            raise

    # -- resume -----------------------------------------------------------

    def reconcile_pending(self) -> list[Outcome]:
        """Materialise ``UNKNOWN`` outcomes for dispatch-without-outcome.

        Called by the runner on resume. Each pending target gets an explicit
        terminal record so the run's accounting is complete and the state
        becomes final. Returns what was written, for reporting to the operator.
        """
        reconciled: list[Outcome] = []
        for target_ref in sorted(self.state.pending):
            intent = self.state._intents[target_ref]
            outcome = Outcome(
                terminal=TerminalState.UNKNOWN,
                target_ref=target_ref,
                channel=intent.channel,
                account_ref=intent.account_ref,
                lease_id=intent.lease_id,
                attempt=intent.attempt,
                resolved_user_id=intent.resolved_user_id,
                dispatched_at=intent.at,
                finished_at=utc_now(),
                detail=(
                    "run ended after dispatch with no outcome recorded; "
                    "not retried because the request may have landed"
                ),
            )
            self.record_outcome(outcome)
            reconciled.append(outcome)
        if reconciled:
            log.warning(
                "%d target(s) were dispatched with no recorded outcome and are "
                "now UNKNOWN. They will not be retried. Review them by hand.",
                len(reconciled),
            )
        return reconciled

    # -- single-writer lock ------------------------------------------------

    @contextmanager
    def exclusive(self) -> Iterator["CheckpointStore"]:
        """Hold an OS lock on the run for the duration of the block.

        The OS releases the lock when the process dies, so a crashed run leaves
        no stale lock to clean up by hand -- the failure mode of a PID-file lock
        that needs a liveness check to reclaim.
        """
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+")
        acquired = False
        try:
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write("0")
                    handle.flush()
            except OSError as exc:
                # Windows denies even opening a locked file for read.
                raise RunAborted(
                    f"another process already holds {lock_path}. "
                    "Refusing to start a second run against the same checkpoint."
                ) from exc

            if sys.platform == "win32":
                import msvcrt

                # On Windows a locked file cannot even be opened for reading by
                # another process, so the read below raises PermissionError
                # rather than returning short. That is still "someone else has
                # it", and it must surface as RunAborted rather than a
                # traceback -- the lock working is not an error condition.
                handle.seek(0)
                try:
                    if not handle.read(1):
                        handle.write("0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    # PermissionError lands here too: once another process holds
                    # the byte-range lock, Windows denies reads on the handle.
                    # That is the lock working, not a fault.
                    acquired = False
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError:
                    acquired = False

            if not acquired:
                raise RunAborted(
                    f"another process already holds {lock_path}. "
                    f"Refusing to start a second run against the same checkpoint."
                )

            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()} {utc_now().isoformat()}\n")
            handle.flush()
            yield self
        finally:
            if acquired:
                handle.seek(0)
                handle.truncate()
                handle.flush()
            handle.close()
