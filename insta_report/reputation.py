"""Persist what a run learned about its accounts, across restarts.

The pool's runtime state -- how much of today's budget is spent, how many
failures an account has piled up, how long it is quarantined for -- lived
only in memory. Every restart therefore reset the daily budget to zero and
the failure streak to zero, which turns a self-imposed budget into a
suggestion: Ctrl-C, run again, twenty more reports. The forum's standing
rule is harsher than this tool was: *reputation is cumulative -- there is
no reset*.

What is written down, per account and keyed by ``ref`` (never by
credential):

* ``used_today`` and ``day`` -- the budget, so it survives a restart on the
  same calendar day. A restart tomorrow resets it anyway, because tomorrow
  it *is* a new day.
* ``consecutive_failures`` -- the streak. It outlives the quarantine that
  accompanied it: the deadline is wall-clock and decays while the tool is
  off, the streak does not, so the next failure backs off from where the
  last run stopped rather than from a fresh five minutes.
* ``quarantined_until_epoch`` -- the deadline, stored as a wall-clock epoch
  because ``time.monotonic()`` from a dead process means nothing to a live
  one. The conversion both ways happens here, against the pool's own clock.
* ``total_reports`` / ``total_acked`` / ``sidelined_for`` -- history, and
  the current reason an account is out of rotation.

A file that cannot be read is a refusal, not a fresh start: starting
anyway would silently clear every budget and streak it holds, which is the
exact hole this file exists to close. The save is best effort -- it runs in
the run's own ``finally``, and a failure warns rather than replacing the
run's own result -- because the checkpoint, not this file, owns the
question of what was dispatched. A hard crash loses the save; that is the
price of not fsyncing per event, taken deliberately.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from .accounts import Account, AccountPool
from .config import ConfigError

__all__ = [
    "FORMAT_VERSION",
    "AccountReputation",
    "ReputationError",
    "ReputationStore",
]

log = logging.getLogger(__name__)

#: Bumped when the on-disk shape changes. A file carrying anything else is
#: refused rather than read: a version field exists precisely so that
#: guessing at state we may misread is worse than not loading it.
FORMAT_VERSION = 1


class ReputationError(ConfigError):
    """A reputation file that cannot be trusted.

    A ``ConfigError`` on purpose: ``main`` already prints those as one-line
    operator-facing refusals with exit code 2, and a malformed state file is
    the same class of event as a malformed config -- the operator's own
    input, unreadable, with one obvious remedy (fix it or delete it).
    """


@dataclass(frozen=True)
class AccountReputation:
    """One account's persisted runtime state.

    Counters, a reason, and a deadline -- and no credential of any kind,
    which is enforced by a test that writes a real-looking ``sessionid``
    through the pool and reads this file back.
    """

    ref: str
    consecutive_failures: int
    total_reports: int
    total_acked: int
    used_today: int
    #: ``date.isoformat()``. Kept as text so the file stays readable by the
    #: operator it is refusing to lie to; parsed and normalised on the way in.
    day: str
    quarantined_until_epoch: float | None = None
    sidelined_for: str | None = None


def _nonneg_int(data: dict, key: str) -> int:
    """One counter, validated. ``bool`` is rejected explicitly because Python
    will not reject it for us: a hand-edited ``true`` must not become a count."""
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer, got {value!r}")
    return value


def _optional_epoch(data: dict, key: str) -> float | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(
            f"{key} must be a non-negative epoch in seconds, got {value!r}"
        )
    return float(value)


def _optional_str(data: dict, key: str) -> str | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    if not isinstance(value, str):
        raise ValueError(
            f"{key} must be a string or absent, got {type(value).__name__}"
        )
    return value


def _record(path: Path, ref: str, data: object) -> AccountReputation:
    """Build one record, refusing anything a hand-edit could have broken.

    Every failure names the file and the account: the operator is about to
    decide whether to fix or delete this file, and a message that does not
    say *which entry* is wrong turns that into a search.
    """
    if not isinstance(data, dict):
        raise ReputationError(
            f"reputation file {path}: account {ref!r} must be a JSON object, "
            f"got {type(data).__name__}"
        )
    try:
        raw_day = data["day"]
        if not isinstance(raw_day, str):
            raise ValueError(f"day must be an ISO date string, got {raw_day!r}")
        try:
            day = date.fromisoformat(raw_day).isoformat()
        except ValueError:
            # Re-raised in our own words: fromisoformat's message never
            # mentions the key it was given, and the operator needs the key.
            raise ValueError(
                f"day must be an ISO date (YYYY-MM-DD), got {raw_day!r}"
            ) from None
        return AccountReputation(
            ref=ref,
            consecutive_failures=_nonneg_int(data, "consecutive_failures"),
            total_reports=_nonneg_int(data, "total_reports"),
            total_acked=_nonneg_int(data, "total_acked"),
            used_today=_nonneg_int(data, "used_today"),
            day=day,
            quarantined_until_epoch=_optional_epoch(data, "quarantined_until_epoch"),
            sidelined_for=_optional_str(data, "sidelined_for"),
        )
    except KeyError as exc:
        raise ReputationError(
            f"reputation file {path}: account {ref!r} is missing {exc.args[0]!r}"
        ) from exc
    except ValueError as exc:
        raise ReputationError(f"reputation file {path}: account {ref!r}: {exc}") from exc


class ReputationStore:
    """Reads and writes the one reputation file a run owns.

    ``wall_clock`` is time in epoch seconds -- the only currency that
    survives a process -- and ``today`` is the local calendar day the budget
    is denominated in. Both are injectable because every conversion in this
    class is otherwise untestable without sleeping or waiting for midnight.
    """

    def __init__(
        self,
        path: Path,
        *,
        wall_clock: Callable[[], float] = time.time,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._path = path
        self._wall_clock = wall_clock
        self._today = today

    def load_into(self, pool: AccountPool) -> list[AccountReputation]:
        """Apply persisted state to *pool*; return the records that were applied.

        A missing file is a first run, not an error. Everything else wrong
        with the file is a refusal: the budgets it holds are the claim under
        review, and silently starting fresh is the failure this module
        exists to prevent.
        """
        if not self._path.exists():
            return []
        if not self._path.is_file():
            raise ReputationError(
                f"reputation file {self._path} exists but is not a regular file"
            )
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ReputationError(
                f"reputation file {self._path} is not valid JSON ({exc}). Fix "
                "or delete it: starting anyway would clear every budget and "
                "failure streak it holds, which is the hole this file closes."
            ) from exc
        except OSError as exc:
            raise ReputationError(
                f"reputation file {self._path} cannot be read: {exc}"
            ) from exc

        records = self._parse(raw)
        today = self._today().isoformat()
        applied: list[AccountReputation] = []
        for ref, record in records.items():
            try:
                account = pool.get(ref)
            except KeyError:
                log.info(
                    "reputation for %r ignored: no such account in config", ref
                )
                continue
            self._apply(account, pool, record, today)
            applied.append(record)
        return applied

    def save(self, pool: AccountPool) -> None:
        """Write every account's state, atomically. Raises on failure.

        Atomic because a half-written reputation file is a corrupt one, and a
        corrupt one is a refusal on the next run -- the write must never be
        able to manufacture the failure the read refuses on. Temp file,
        fsync, replace: a reader sees the old file or the new one, never a
        truncated one.
        """
        wall_now = self._wall_clock()
        mono_now = pool.monotonic()
        entries: dict[str, object] = {}
        for account in pool.accounts:
            entry: dict[str, object] = {
                "consecutive_failures": account.consecutive_failures,
                "total_reports": account.total_reports,
                "total_acked": account.total_acked,
                "used_today": account.used_today,
                "day": account.day.isoformat(),
            }
            if account.quarantined_until is not None:
                remaining = account.quarantined_until - mono_now
                if remaining > 0:
                    # Monotonic in, epoch out -- the other direction of the
                    # bridge in _apply. An expired deadline is simply not
                    # written; the streak that produced it still is.
                    entry["quarantined_until_epoch"] = wall_now + remaining
            if account.sidelined_for:
                entry["sidelined_for"] = account.sidelined_for
            entries[account.ref] = entry
        payload = {
            "version": FORMAT_VERSION,
            "saved_at": wall_now,
            "accounts": entries,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            # Best-effort cleanup: a leftover temp file is litter, but the
            # litter must never mask the failure that got us here.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _parse(self, raw: object) -> dict[str, AccountReputation]:
        if not isinstance(raw, dict):
            raise ReputationError(
                f"reputation file {self._path}: expected a JSON object, got "
                f"{type(raw).__name__}"
            )
        version = raw.get("version")
        if version != FORMAT_VERSION:
            raise ReputationError(
                f"reputation file {self._path}: version {version!r} is not the "
                f"format this tool writes ({FORMAT_VERSION}). Refusing to guess "
                "at state it may misread."
            )
        accounts = raw.get("accounts")
        if not isinstance(accounts, dict):
            raise ReputationError(
                f"reputation file {self._path}: 'accounts' must be a JSON "
                f"object, got {type(accounts).__name__}"
            )
        return {ref: _record(self._path, ref, data) for ref, data in accounts.items()}

    def _apply(
        self,
        account: Account,
        pool: AccountPool,
        record: AccountReputation,
        today: str,
    ) -> None:
        account.consecutive_failures = record.consecutive_failures
        account.total_reports = record.total_reports
        account.total_acked = record.total_acked
        account.sidelined_for = record.sidelined_for

        if record.day == today:
            account.used_today = record.used_today
        else:
            log.info(
                "account %s: saved budget is from %s, not today; the daily "
                "budget starts fresh (the failure streak still counts)",
                account.ref,
                record.day,
            )

        if record.quarantined_until_epoch is None:
            return
        remaining = record.quarantined_until_epoch - self._wall_clock()
        if remaining > 0:
            # Epoch in, monotonic out: the deadline is re-anchored to THIS
            # process's clock, because time.monotonic() from the last run has
            # no meaning to this one -- and it must be the pool's clock, the
            # same one eligibility is judged with.
            account.quarantined_until = pool.monotonic() + remaining
        else:
            log.info(
                "account %s: quarantine lapsed %.0fs while the tool was off; "
                "the streak of %d is kept, so the next failure still backs off "
                "from where the last run stopped",
                account.ref,
                -remaining,
                account.consecutive_failures,
            )
